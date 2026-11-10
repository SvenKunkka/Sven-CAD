// SPDX-License-Identifier: LGPL-2.1-or-later

#include "FeatureInternalThread.h"

#include <algorithm>
#include <cmath>
#include <utility>

#include <BRepClass3d_SolidClassifier.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <GProp_GProps.hxx>
#include <Precision.hxx>
#include <Standard_Failure.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax3.hxx>

#include <App/GeoFeatureGroupExtension.h>
#include <Base/Exception.h>
#include <Mod/Part/App/FCBRepAlgoAPI_Common.h>

#include "FeatureExternalThread.h"
#include "FeatureHole.h"
#include "SvenMetricThreadLimits.h"

using namespace PartDesign;

PROPERTY_SOURCE(PartDesign::InternalThread, Part::Feature)

namespace
{
double volume(const TopoDS_Shape& shape)
{
    if (shape.IsNull()) {
        return 0.0;
    }
    GProp_GProps properties;
    BRepGProp::VolumeProperties(shape, properties, true);
    return properties.Mass();
}

Part::TopoShape makeGroove(double majorRadius, double pitch, double length, bool leftHanded)
{
    Part::TopoShape helix(Part::TopoShape().makeLongHelix(
        pitch, length + 2 * pitch, majorRadius, 0, leftHanded
    ));
    gp_Trsf leadIn;
    leadIn.SetTranslation(gp_Vec(0, 0, -pitch));
    helix.move(leadIn);
    auto wire = Hole::metricThreadProfile(majorRadius, pitch, gp_Vec(1, 0, 0), gp_Vec(0, 0, 1));
    wire.Move(TopLoc_Location(leadIn));
    TopTools_ListOfShape profiles;
    profiles.Append(wire);
    auto sweep = helix.makePipeShell(profiles, true, true);
    if (sweep.IsNull() || sweep.ShapeType() != TopAbs_SOLID) {
        throw Base::CADKernelError(QT_TRANSLATE_NOOP("Exception", "The internal thread groove could not be built."));
    }
    BRepClass3d_SolidClassifier classifier(sweep);
    classifier.PerformInfinitePoint(Precision::Confusion());
    if (classifier.State() == TopAbs_IN) {
        sweep.Reverse();
    }
    const auto envelope = BRepPrimAPI_MakeCylinder(majorRadius + pitch / 32, length).Shape();
    FCBRepAlgoAPI_Common trim(sweep, envelope);
    if (!trim.IsDone() || trim.Shape().IsNull()) {
        throw Base::CADKernelError(QT_TRANSLATE_NOOP("Exception", "The internal thread groove could not be built."));
    }
    return Part::TopoShape(trim.Shape());
}
}  // namespace

InternalThread::InternalThread()
{
    ADD_PROPERTY_TYPE(Support, (nullptr), "Thread", App::Prop_None, "Selected internal cylindrical wall");
    ADD_PROPERTY_TYPE(ThreadSize, (2L), "Thread", App::Prop_None, "Nominal metric coarse thread size");
    ThreadSize.setEnums(ExternalThread::Sizes);
    ThreadSize.setValue(2L);
    ADD_PROPERTY_TYPE(ThreadLength, (2.0), "Thread", App::Prop_None, "Length from the selected hole opening");
    ADD_PROPERTY_TYPE(LeftHanded, (false), "Thread", App::Prop_None, "Use a left-hand thread");
    ADD_PROPERTY_TYPE(Modeled, (true), "Thread", App::Prop_None, "Cut the thread groove into the solid");
    ADD_PROPERTY_TYPE(ReverseStart, (false), "Thread", App::Prop_None, "Use the opposite opening of a through hole");
    ADD_PROPERTY_TYPE(Pitch, (0.0), "Thread", App::Prop_ReadOnly, "Nominal thread pitch");
    ADD_PROPERTY_TYPE(MajorDiameter, (0.0), "Thread", App::Prop_ReadOnly, "Nominal thread major diameter");
    ADD_PROPERTY_TYPE(BoreDiameter, (0.0), "Thread", App::Prop_ReadOnly, "Diameter of the original bore");
    ADD_PROPERTY_TYPE(ManufacturingClass, ("6H"), "Manufacturing", App::Prop_ReadOnly, "Drawing tolerance class");
    ADD_PROPERTY_TYPE(ManufacturingLimits, (""), "Manufacturing", App::Prop_ReadOnly,
                      "Reference diameter limits and normal engagement length");
}

InternalThread::HoleWall InternalThread::describe(const Part::Feature& source, const char* faceName)
{
    auto bore = Part::SvenHoleResize::describe(source, faceName);
    if (!bore.startOpen && !bore.endOpen) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "The hole must have an open end."));
    }
    for (int size = 0; ExternalThread::Sizes[size]; ++size) {
        const auto* limits = MetricCoarseThreadLimits::find(ExternalThread::Sizes[size]);
        const double diameter = 2 * bore.radius;
        if (limits && diameter >= limits->internalMinor.min - Precision::Confusion()
            && diameter <= limits->internalMinor.max + Precision::Confusion()) {
            return {std::move(bore), size};
        }
    }
    throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "The bore diameter must fit the 6H minor diameter range for M2, M2.5, M3, or M4."));
}

Part::TopoShape InternalThread::makeShape(const HoleWall& bore, int size, double length, bool leftHanded,
                                       bool modeled, bool reverseStart)
{
    if (size != bore.size) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "The selected size does not match the bore diameter."));
    }
    if (!std::isfinite(length) || length <= Precision::Confusion()
        || length > bore.depth + Precision::Confusion()) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Thread length must be positive and fit the cylindrical face."));
    }
    if (reverseStart && !(bore.startOpen && bore.endOpen)) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Only a through hole can use the opposite opening."));
    }
    if (!modeled) {
        return bore.shape;
    }
    const double pitch = ExternalThread::pitchForSize(size);
    if (length > ExternalThread::MaxModeledTurns * pitch + Precision::Confusion()) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Modeled threads are limited to 64 turns."));
    }
    const double effectiveLength = std::min(length, bore.depth);
    auto groove = makeGroove(ExternalThread::majorDiameterForSize(size) / 2, pitch, effectiveLength, leftHanded);
    const Part::TopoShape existingBore(BRepPrimAPI_MakeCylinder(bore.radius, effectiveLength).Shape());
    const auto cutMaterial = groove.makeElementCut(existingBore);
    const double expected = volume(cutMaterial.getShape());
    const double tolerance = std::max(expected * 1e-6, 1e-6);

    auto start = bore.start.Location();
    auto direction = bore.start.Direction();
    if (!bore.startOpen || reverseStart) {
        start.Translate(gp_Vec(direction) * bore.depth);
        direction.Reverse();
    }
    gp_Trsf placement;
    placement.SetDisplacement(gp_Ax3(), gp_Ax3(start, direction, bore.start.XDirection()));
    groove.move(placement);
    auto result = bore.shape.makeElementCut(groove, "InternalThread");
    const double removed = volume(bore.shape.getShape()) - volume(result.getShape());
    if (expected <= Precision::Confusion() || result.isNull() || result.countSubShapes(TopAbs_SOLID) != 1
        || !result.isValid() || std::abs(expected - removed) > tolerance) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "The thread groove would break through the hole wall or intersect nearby geometry."));
    }
    return result;
}

void InternalThread::setThreadResult(Part::Feature& source, const HoleWall& bore, const Part::TopoShape& shape)
{
    Pitch.setValue(ExternalThread::pitchForSize(bore.size));
    MajorDiameter.setValue(ExternalThread::majorDiameterForSize(bore.size));
    BoreDiameter.setValue(2 * bore.radius);
    ManufacturingLimits.setValue(MetricCoarseThreadLimits::find(ExternalThread::Sizes[bore.size])->internalReference());
    Placement.setValue(source.Placement.getValue());
    auto placed = shape;
    placed.setTransform(Placement.getValue().toMatrix());
    Shape.setValue(placed);
    copyMaterial(&source);
}

short InternalThread::mustExecute() const
{
    if (Support.isTouched() || ThreadSize.isTouched() || ThreadLength.isTouched()
        || LeftHanded.isTouched() || Modeled.isTouched() || ReverseStart.isTouched()) {
        return 1;
    }
    return Part::Feature::mustExecute();
}

App::DocumentObjectExecReturn* InternalThread::execute()
{
    auto* source = freecad_cast<Part::Feature*>(Support.getValue());
    const auto faces = Support.getSubValues(true);
    if (!source || source == this || faces.size() != 1 || source->getDocument() != getDocument()
        || App::GeoFeatureGroupExtension::getGroupOfObject(source)
            != App::GeoFeatureGroupExtension::getGroupOfObject(this)) {
        return new App::DocumentObjectExecReturn(
            QT_TRANSLATE_NOOP("Exception", "Select one internal cylindrical wall in the same part.")
        );
    }
    try {
        const auto bore = describe(*source, faces.front().c_str());
        const auto shape = makeShape(bore, static_cast<int>(ThreadSize.getValue()), ThreadLength.getValue(),
                                     LeftHanded.getValue(), Modeled.getValue(), ReverseStart.getValue());
        setThreadResult(*source, bore, shape);
        return Part::Feature::execute();
    }
    catch (const Base::Exception& error) {
        return new App::DocumentObjectExecReturn(error.what());
    }
    catch (const Standard_Failure&) {
        return new App::DocumentObjectExecReturn(
            QT_TRANSLATE_NOOP("Exception", "The geometry kernel could not build the internal thread.")
        );
    }
}
