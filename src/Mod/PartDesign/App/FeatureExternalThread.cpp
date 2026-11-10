// SPDX-License-Identifier: LGPL-2.1-or-later

#include "FeatureExternalThread.h"
#include "SvenMetricThreadLimits.h"

#include <algorithm>
#include <cmath>
#include <iterator>
#include <numbers>
#include <string_view>

#include <QCoreApplication>

#include <BRepAdaptor_Surface.hxx>
#include <BRepAlgoAPI_Common.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepClass3d_SolidClassifier.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <GC_MakeArcOfCircle.hxx>
#include <GProp_GProps.hxx>
#include <Geom_TrimmedCurve.hxx>
#include <Precision.hxx>
#include <Standard_Failure.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Iterator.hxx>

#include <App/GeoFeatureGroupExtension.h>
#include <Base/Exception.h>

#include "FeatureHole.h"

using namespace PartDesign;

PROPERTY_SOURCE(PartDesign::ExternalThread, Part::Feature)

const char* ExternalThread::Sizes[] = {"M2x0.4", "M2.5x0.45", "M3x0.5", "M4x0.7", nullptr};

namespace
{
const Hole::ThreadDescription& nominal(int size)
{
    if (size < 0 || size >= static_cast<int>(std::size(ExternalThread::Sizes)) - 1) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Select a supported metric thread size."));
    }
    const auto& coarse = Hole::threadDescription[1];
    const auto item = std::ranges::find_if(coarse, [size](const auto& entry) {
        return std::string_view(entry.designation) == ExternalThread::Sizes[size];
    });
    if (item == coarse.end()) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Select a supported metric thread size."));
    }
    return *item;
}

// ISO 68-1 and ISO 724 basic profile, without a manufacturing allowance.
double fundamentalHeight(double pitch)
{
    return std::sqrt(3.0) * pitch / 2.0;
}

double rootRadius(double majorRadius, double pitch)
{
    return majorRadius - 17.0 * fundamentalHeight(pitch) / 24.0;
}

double volume(const TopoDS_Shape& shape)
{
    GProp_GProps properties;
    BRepGProp::VolumeProperties(shape, properties, true);
    return properties.Mass();
}

TopoDS_Shape singleSolid(TopoDS_Shape shape)
{
    while (!shape.IsNull() && shape.ShapeType() == TopAbs_COMPOUND) {
        TopoDS_Iterator children(shape);
        if (!children.More()) {
            return {};
        }
        auto child = children.Value();
        children.Next();
        if (children.More()) {
            return {};
        }
        shape = child;
    }
    if (shape.IsNull() || shape.ShapeType() != TopAbs_SOLID) {
        return {};
    }
    return shape;
}

TopoDS_Wire grooveProfile(double radius, double pitch)
{
    const double h = fundamentalHeight(pitch);
    const double outer = radius + pitch / 32.0;
    const double halfWidth = 7.0 * pitch / 16.0 + (outer - radius) / std::sqrt(3.0);
    const gp_Pnt outerStart(outer, 0.0, -pitch - halfWidth);
    const gp_Pnt rootStart(radius - 5.0 * h / 8.0, 0.0, -pitch - pitch / 8.0);
    const gp_Pnt rootMid(rootRadius(radius, pitch), 0.0, -pitch);
    const gp_Pnt rootEnd(radius - 5.0 * h / 8.0, 0.0, -pitch + pitch / 8.0);
    const gp_Pnt outerEnd(outer, 0.0, -pitch + halfWidth);
    const auto arc = GC_MakeArcOfCircle(rootStart, rootMid, rootEnd).Value();
    return BRepBuilderAPI_MakeWire(
        BRepBuilderAPI_MakeEdge(outerStart, rootStart),
        BRepBuilderAPI_MakeEdge(arc),
        BRepBuilderAPI_MakeEdge(rootEnd, outerEnd),
        BRepBuilderAPI_MakeEdge(outerEnd, outerStart)
    ).Wire();
}

Part::TopoShape makeCutter(const ExternalThread::Shaft& shaft, double pitch, double length, bool leftHanded)
{
    Part::TopoShape helix(Part::TopoShape().makeLongHelix(
        pitch, length + 2.0 * pitch, shaft.radius, 0.0, leftHanded
    ));
    gp_Trsf start;
    start.SetTranslation(gp_Vec(0.0, 0.0, -pitch));
    helix.move(start);
    TopTools_ListOfShape profiles;
    profiles.Append(grooveProfile(shaft.radius, pitch));
    auto groove = helix.makePipeShell(profiles, true, true);
    if (singleSolid(groove).IsNull()) {
        throw Base::CADKernelError(QT_TRANSLATE_NOOP("Exception", "The thread groove could not be built."));
    }
    BRepClass3d_SolidClassifier classifier(groove);
    classifier.PerformInfinitePoint(Precision::Confusion());
    if (classifier.State() == TopAbs_IN) {
        groove.Reverse();
    }
    // The sweep extends beyond both ends for a full thread profile. Clipping
    // limits it to the requested shaft interval and protects nearby geometry.
    const auto envelope = BRepPrimAPI_MakeCylinder(shaft.radius, length).Shape();
    BRepAlgoAPI_Common trim(groove, envelope);
    if (!trim.IsDone() || trim.Shape().IsNull() || volume(trim.Shape()) <= Precision::Confusion()) {
        throw Base::CADKernelError(QT_TRANSLATE_NOOP("Exception", "The thread groove could not be built."));
    }
    gp_Trsf placement;
    placement.SetDisplacement(gp_Ax3(), shaft.axes);
    Part::TopoShape cutter(trim.Shape());
    cutter.move(placement);
    return cutter;
}
}  // namespace

ExternalThread::ExternalThread()
{
    ADD_PROPERTY_TYPE(Support, (nullptr), "Thread", App::Prop_None, "Selected exterior cylindrical face");
    ADD_PROPERTY_TYPE(ThreadSize, (2L), "Thread", App::Prop_None, "Nominal metric coarse thread size");
    ThreadSize.setEnums(Sizes);
    ThreadSize.setValue(2L);
    ADD_PROPERTY_TYPE(ThreadLength, (3.0), "Thread", App::Prop_None, "Length from the start of the cylinder");
    ADD_PROPERTY_TYPE(LeftHanded, (false), "Thread", App::Prop_None, "Use a left-hand thread");
    ADD_PROPERTY_TYPE(Modeled, (true), "Thread", App::Prop_None, "Cut the thread groove into the solid");
    ADD_PROPERTY_TYPE(ManufacturingLimits, (""), "Manufacturing",
                      static_cast<App::PropertyType>(App::Prop_ReadOnly | App::Prop_Output),
                      "Reference diameter limits and normal engagement length");
    ADD_PROPERTY_TYPE(ManufacturingClass, (0L), "Manufacturing", App::Prop_NoRecompute,
                      "Drawing tolerance class. The model uses nominal geometry.");
    ManufacturingClass.setEnums(ManufacturingClasses);
    ADD_PROPERTY_TYPE(Pitch, (0.5), "Thread", App::Prop_ReadOnly, "Nominal thread pitch");
    ADD_PROPERTY_TYPE(MajorDiameter, (3.0), "Thread", App::Prop_ReadOnly, "Nominal major diameter");
    ADD_PROPERTY_TYPE(MinorDiameter, (2.0 * rootRadius(1.5, 0.5)), "Thread", App::Prop_ReadOnly, "Nominal minor diameter");
    ADD_PROPERTY_TYPE(ThreadFrame, (Base::Placement()), "Thread",
                      static_cast<App::PropertyType>(App::Prop_ReadOnly | App::Prop_Output), "Thread start on the axis in the feature coordinates. Z points along the thread.");
}

const char* ExternalThread::ManufacturingClasses[] = {"Unspecified", "6g", nullptr};

void ExternalThread::updateManufacturingLimits()
{
    const auto* limits = ThreadSize.isValid() && ManufacturingClass.isValid()
        && std::string_view(ManufacturingClass.getValueAsString()) == "6g"
        ? MetricCoarseThreadLimits::find(ThreadSize.getValueAsString()) : nullptr;
    const auto value = limits ? limits->externalReference() : std::string();
    if (ManufacturingLimits.getStrValue() != value) {
        ManufacturingLimits.setValue(value);
    }
}

void ExternalThread::onChanged(const App::Property* property)
{
    if (property == &ThreadSize || property == &ManufacturingClass) {
        updateManufacturingLimits();
    }
    Part::Feature::onChanged(property);
}

void ExternalThread::onDocumentRestored()
{
    Part::Feature::onDocumentRestored();
    updateManufacturingLimits();
}

ExternalThread::Shaft ExternalThread::describe(const Part::Feature& source, const char* faceName)
{
    auto shape = source.Shape.getShape();
    shape.setTransform(Base::Matrix4D());
    auto solid = singleSolid(shape.getShape());
    if (solid.IsNull() || !shape.isValid() || volume(solid) <= Precision::Confusion()) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Select a face of a valid single solid."));
    }
    auto faceShape = shape.getSubShape(faceName, true);
    if (faceShape.IsNull() || faceShape.ShapeType() != TopAbs_FACE) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Select one exterior cylindrical face."));
    }
    auto face = TopoDS::Face(faceShape);
    BRepAdaptor_Surface surface(face);
    if (surface.GetType() != GeomAbs_Cylinder
        || std::abs(surface.LastUParameter() - surface.FirstUParameter() - 2.0 * std::numbers::pi) > 1e-6) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Select one full exterior cylindrical face."));
    }
    const auto cylinder = surface.Cylinder();
    const auto axis = cylinder.Axis().Direction();
    const double start = surface.FirstVParameter();
    const double end = surface.LastVParameter();
    const double length = end - start;
    const double radius = cylinder.Radius();
    const auto origin = cylinder.Location().Translated(gp_Vec(axis) * start);
    GProp_GProps area;
    BRepGProp::SurfaceProperties(face, area);
    const double expectedArea = 2.0 * std::numbers::pi * radius * length;
    if (!std::isfinite(expectedArea) || length <= Precision::Confusion()
        || std::abs(area.Mass() - expectedArea) > expectedArea * 1e-6) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Select one full exterior cylindrical face."));
    }
    gp_Pnt surfacePoint;
    gp_Vec du;
    gp_Vec dv;
    surface.D1((surface.FirstUParameter() + surface.LastUParameter()) / 2.0,
               (start + end) / 2.0, surfacePoint, du, dv);
    auto normal = du.Crossed(dv);
    if (face.Orientation() == TopAbs_REVERSED) {
        normal.Reverse();
    }
    if (normal.Dot(gp_Vec(cylinder.Location(), surfacePoint)) <= 0.0) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Select an exterior shaft, not a hole."));
    }
    for (int size = 0; Sizes[size]; ++size) {
        if (std::abs(nominal(size).diameter - 2.0 * radius) <= 1e-4) {
            return {shape, gp_Ax3(origin, axis, cylinder.Position().XDirection()), length, radius, size};
        }
    }
    throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "The shaft diameter does not match M2, M2.5, M3, or M4."));
}

double ExternalThread::pitchForSize(int size)
{
    return nominal(size).pitch;
}

double ExternalThread::majorDiameterForSize(int size)
{
    return nominal(size).diameter;
}

Part::TopoShape ExternalThread::makeShape(
    const Shaft& shaft, int size, double length, bool leftHanded, bool modeled
)
{
    if (size != shaft.size) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "The selected size does not match the shaft diameter."));
    }
    if (!std::isfinite(length) || length <= Precision::Confusion()
        || length > shaft.length + Precision::Confusion()) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Thread length must be positive and fit the cylindrical face."));
    }
    if (!modeled) {
        return shaft.shape;
    }
    const auto pitch = pitchForSize(size);
    if (length > MaxModeledTurns * pitch + Precision::Confusion()) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Modeled threads are limited to 64 turns."));
    }
    auto cutter = makeCutter(shaft, pitch, std::min(length, shaft.length), leftHanded);
    auto result = shaft.shape.makeElementCut(cutter, "ExternalThread");
    auto solid = singleSolid(result.getShape());
    const double remaining = solid.IsNull() ? 0.0 : volume(solid);
    const double removed = volume(shaft.shape.getShape()) - remaining;
    if (remaining <= Precision::Confusion() || !result.isValid()
        || removed <= Precision::Confusion()) {
        throw Base::CADKernelError(QT_TRANSLATE_NOOP("Exception", "The thread did not produce a valid single solid."));
    }
    const double grooveVolume = volume(cutter.getShape());
    if (std::abs(removed - grooveVolume) > std::max(1e-6, grooveVolume * 1e-4)) {
        throw Base::CADKernelError(
            QT_TRANSLATE_NOOP("Exception", "The shaft wall is too thin for the complete thread groove.")
        );
    }
    return result;
}

short ExternalThread::mustExecute() const
{
    if (Support.isTouched() || ThreadSize.isTouched() || ThreadLength.isTouched()
        || LeftHanded.isTouched() || Modeled.isTouched()) {
        return 1;
    }
    return Part::Feature::mustExecute();
}

void ExternalThread::setThreadResult(Part::Feature& source, const Part::TopoShape& shape)
{
    const auto& spec = nominal(static_cast<int>(ThreadSize.getValue()));
    Pitch.setValue(spec.pitch);
    MajorDiameter.setValue(spec.diameter);
    MinorDiameter.setValue(2.0 * rootRadius(spec.diameter / 2.0, spec.pitch));
    Placement.setValue(source.Placement.getValue());
    auto placedShape = shape;
    placedShape.setTransform(Placement.getValue().toMatrix());
    Shape.setValue(placedShape);
    copyMaterial(&source);
}

App::DocumentObjectExecReturn* ExternalThread::execute()
{
    auto* source = freecad_cast<Part::Feature*>(Support.getValue());
    const auto faces = Support.getSubValues(true);
    if (!source || source == this || faces.size() != 1 || source->getDocument() != getDocument()
        || App::GeoFeatureGroupExtension::getGroupOfObject(source)
            != App::GeoFeatureGroupExtension::getGroupOfObject(this)) {
        return new App::DocumentObjectExecReturn(
            QT_TRANSLATE_NOOP("Exception", "Select one exterior cylindrical face in the same part.")
        );
    }
    try {
        const auto shaft = describe(*source, faces.front().c_str());
        const auto size = static_cast<int>(ThreadSize.getValue());
        auto result = makeShape(shaft, size, ThreadLength.getValue(), LeftHanded.getValue(), Modeled.getValue());
        setThreadResult(*source, result);
        const auto& origin = shaft.axes.Location();
        const auto& axis = shaft.axes.Direction();
        ThreadFrame.setValue(Base::Placement(Base::Vector3d(origin.X(), origin.Y(), origin.Z()),
                                             Base::Rotation(Base::Vector3d(0, 0, 1),
                                                            Base::Vector3d(axis.X(), axis.Y(), axis.Z()))));
        return Part::Feature::execute();
    }
    catch (const Base::Exception& error) {
        return new App::DocumentObjectExecReturn(error.what());
    }
    catch (const Standard_Failure&) {
        return new App::DocumentObjectExecReturn(
            QT_TRANSLATE_NOOP("Exception", "The geometry kernel could not build the external thread.")
        );
    }
}
