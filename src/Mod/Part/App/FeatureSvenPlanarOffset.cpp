// SPDX-License-Identifier: LGPL-2.1-or-later

#include "FeatureSvenPlanarOffset.h"

#include <algorithm>
#include <cmath>

#include <BRepAdaptor_Surface.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Pln.hxx>
#include <Precision.hxx>
#include <Standard_Failure.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Iterator.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <BRep_Tool.hxx>

#include <App/GeoFeatureGroupExtension.h>
#include <App/Part.h>
#include <Base/Exception.h>

using namespace Part;

namespace
{

constexpr double angularTolerance = 1e-6;

// STEP imports and Boolean results can wrap a solid in a compound.
bool isSingleSolid(TopoDS_Shape shape)
{
    while (!shape.IsNull() && shape.ShapeType() == TopAbs_COMPOUND) {
        TopoDS_Iterator children(shape);
        if (!children.More()) {
            return false;
        }
        shape = children.Value();
        children.Next();
        if (children.More()) {
            return false;
        }
    }
    return !shape.IsNull() && shape.ShapeType() == TopAbs_SOLID;
}

gp_Dir outwardNormal(const TopoDS_Face& face, const gp_Pln& plane)
{
    gp_Dir normal = plane.Axis().Direction();
    if (face.Orientation() == TopAbs_REVERSED) {
        normal.Reverse();
    }
    return normal;
}

bool isPrismaticSide(const TopoDS_Face& side, const gp_Dir& normal)
{
    BRepAdaptor_Surface surface(side);
    switch (surface.GetType()) {
        case GeomAbs_Plane:
            return surface.Plane().Axis().Direction().IsNormal(normal, angularTolerance);
        case GeomAbs_Cylinder:
            return surface.Cylinder().Axis().Direction().IsParallel(normal, angularTolerance);
        default:
            return false;
    }
}

bool hasPrismaticBoundary(
    const TopoDS_Shape& solid,
    const TopoDS_Face& face,
    const gp_Pln& plane,
    const gp_Dir& normal,
    double offset,
    double tolerance
)
{
    TopTools_IndexedDataMapOfShapeListOfShape facesByEdge;
    TopExp::MapShapesAndAncestors(solid, TopAbs_EDGE, TopAbs_FACE, facesByEdge);
    for (TopExp_Explorer edges(face, TopAbs_EDGE); edges.More(); edges.Next()) {
        const auto& adjacent = facesByEdge.FindFromKey(edges.Current());
        if (adjacent.Extent() != 2) {
            return false;
        }
        const auto& side = adjacent.First().IsSame(face) ? adjacent.Last() : adjacent.First();
        if (side.IsSame(face) || !isPrismaticSide(TopoDS::Face(side), normal)) {
            return false;
        }
        if (offset < 0) {
            double inwardExtent = 0;
            for (TopExp_Explorer vertices(side, TopAbs_VERTEX); vertices.More(); vertices.Next()) {
                const auto point = BRep_Tool::Pnt(TopoDS::Vertex(vertices.Current()));
                inwardExtent = std::max(inwardExtent, -gp_Vec(plane.Location(), point).Dot(gp_Vec(normal)));
            }
            if (-offset >= inwardExtent - tolerance) {
                return false;
            }
        }
    }
    return true;
}

double volume(const TopoDS_Shape& shape)
{
    GProp_GProps props;
    BRepGProp::VolumeProperties(shape, props);
    return props.Mass();
}

bool hasMovedFace(
    const TopoDS_Shape& result,
    const gp_Pnt& expectedCenter,
    const gp_Dir& normal,
    double area,
    double tolerance
)
{
    for (TopExp_Explorer faces(result, TopAbs_FACE); faces.More(); faces.Next()) {
        const auto& face = TopoDS::Face(faces.Current());
        BRepAdaptor_Surface surface(face);
        if (surface.GetType() != GeomAbs_Plane) {
            continue;
        }
        const auto plane = surface.Plane();
        if (!outwardNormal(face, plane).IsEqual(normal, angularTolerance)
            || plane.Distance(expectedCenter) > tolerance) {
            continue;
        }
        GProp_GProps props;
        BRepGProp::SurfaceProperties(face, props);
        if (props.CentreOfMass().Distance(expectedCenter) <= tolerance
            && std::abs(props.Mass() - area) <= tolerance * std::sqrt(area)) {
            return true;
        }
    }
    return false;
}

}  // namespace

PROPERTY_SOURCE(Part::SvenPlanarOffset, Part::Feature)

SvenPlanarOffset::SvenPlanarOffset()
{
    ADD_PROPERTY_TYPE(Support, (nullptr), "Offset", App::Prop_None, "Source solid and planar face");
    ADD_PROPERTY_TYPE(Offset, (1.0), "Offset", App::Prop_None, "Signed distance from the source face");
    Placement.setStatus(App::Property::ReadOnly, true);
}

bool SvenPlanarOffset::supports(const App::DocumentObject* source)
{
    if (!source || !source->isAttachedToDocument() || source->getTypeId() != Feature::getClassTypeId()) {
        return false;
    }
    for (auto* parent = App::GeoFeatureGroupExtension::getGroupOfObject(source); parent;
         parent = App::GeoFeatureGroupExtension::getGroupOfObject(parent)) {
        if (parent->getTypeId() != App::Part::getClassTypeId()) {
            return false;
        }
    }
    return true;
}

TopoShape SvenPlanarOffset::calculate(const Feature& source, const char* faceName, double offset)
{
    const double tolerance = 10 * Precision::Confusion();
    if (!std::isfinite(offset) || std::abs(offset) <= tolerance) {
        throw Base::ValueError("Enter a non-zero offset distance.");
    }

    auto solid = source.Shape.getShape();
    solid.locate(TopLoc_Location());
    const auto& base = solid.getShape();
    if (!isSingleSolid(base) || !BRepCheck_Analyzer(base).IsValid()) {
        throw Base::ValueError("Select a face of one valid solid.");
    }
    auto faceShape = solid.getSubTopoShape(faceName, true);
    if (faceShape.isNull() || faceShape.getShape().ShapeType() != TopAbs_FACE) {
        throw Base::ValueError("The source face could not be resolved.");
    }
    const auto& face = TopoDS::Face(faceShape.getShape());
    BRepAdaptor_Surface surface(face);
    if (surface.GetType() != GeomAbs_Plane) {
        throw Base::ValueError("Select one planar face.");
    }
    const auto plane = surface.Plane();
    const auto normal = outwardNormal(face, plane);
    if (!hasPrismaticBoundary(base, face, plane, normal, offset, tolerance)) {
        throw Base::ValueError("Adjacent faces must extend in the offset direction for the entire distance.");
    }

    GProp_GProps faceProperties;
    BRepGProp::SurfaceProperties(face, faceProperties);
    const double area = faceProperties.Mass();
    const double expectedVolumeChange = area * offset;
    const double sourceVolume = volume(base);
    if (area <= tolerance * tolerance || sourceVolume <= 0
        || !std::isfinite(expectedVolumeChange) || sourceVolume + expectedVolumeChange <= 0) {
        throw Base::ValueError("The offset would consume the solid.");
    }

    const auto displacement = gp_Vec(normal) * offset;
    const auto prism = faceShape.makeElementPrism(displacement);
    const auto result = (offset > 0 ? solid.makeElementFuse(prism) : solid.makeElementCut(prism))
                            .makeElementRefine();
    const auto& resultShape = result.getShape();
    if (!isSingleSolid(resultShape) || !BRepCheck_Analyzer(resultShape).IsValid()) {
        throw Base::ValueError("The offset would not produce one valid solid.");
    }
    const double volumeChange = volume(resultShape) - sourceVolume;
    const double volumeTolerance = std::max(area * tolerance, std::abs(expectedVolumeChange) * 1e-7);
    if (volumeChange * expectedVolumeChange <= 0
        || std::abs(volumeChange - expectedVolumeChange) > volumeTolerance
        || !hasMovedFace(resultShape, faceProperties.CentreOfMass().Translated(displacement),
                         normal, area, tolerance)) {
        throw Base::ValueError("The offset intersects other geometry or crosses the opposite wall.");
    }
    return result;
}

short SvenPlanarOffset::mustExecute() const
{
    return Support.isTouched() || Offset.isTouched() || Feature::mustExecute();
}

App::DocumentObjectExecReturn* SvenPlanarOffset::execute()
{
    auto* source = Support.getValue();
    const auto faces = Support.getSubValues(true);
    if (!supports(source) || source->getDocument() != getDocument() || faces.size() != 1
        || App::GeoFeatureGroupExtension::getGroupOfObject(source)
            != App::GeoFeatureGroupExtension::getGroupOfObject(this)) {
        return new App::DocumentObjectExecReturn("Select one supported face in the same part.");
    }
    try {
        auto* feature = static_cast<Feature*>(source);
        auto result = calculate(*feature, faces.front().c_str(), Offset.getValue());
        Placement.setValueIfChanged(feature->Placement.getValue());
        copyMaterial(feature);
        Shape.setValue(result);
        return App::DocumentObject::StdReturn;
    }
    catch (const Base::Exception& error) {
        return new App::DocumentObjectExecReturn(error.what());
    }
    catch (const Standard_Failure& error) {
        return new App::DocumentObjectExecReturn(
            error.GetMessageString() ? error.GetMessageString() : "The planar offset failed."
        );
    }
}
