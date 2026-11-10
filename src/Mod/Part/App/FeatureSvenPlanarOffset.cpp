// SPDX-License-Identifier: LGPL-2.1-or-later

#include "FeatureSvenPlanarOffset.h"

#include <algorithm>
#include <cmath>
#include <numbers>
#include <sstream>

#include <QCryptographicHash>

#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <GProp_GProps.hxx>
#include <gp_Lin.hxx>
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
#include <App/ElementMap.h>
#include <App/Part.h>
#include <Base/Exception.h>

using namespace Part;

namespace
{

constexpr double angularTolerance = 1e-6;
const double holeTolerance = 10 * Precision::Confusion();

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

TopoShape copySolidAndFace(const Feature& source, const char* name, TopoShape& face)
{
    const auto& shape = source.Shape.getShape();
    const auto selected = shape.getSubTopoShape(name, true);
    const int index = selected.isNull() ? 0 : shape.findShape(selected.getShape());
    if (index <= 0 || selected.getShape().ShapeType() != TopAbs_FACE) {
        throw Base::ValueError("The source face could not be resolved.");
    }
    // OCCT builders can update shared edge curves. Work on independent geometry.
    auto copy = shape.makeElementCopy();
    copy.locate(TopLoc_Location());
    if (!isSingleSolid(copy.getShape()) || !BRepCheck_Analyzer(copy.getShape()).IsValid()) {
        throw Base::ValueError("Select a face of one valid solid.");
    }
    face = copy.getSubTopoShape(TopAbs_FACE, index);
    return copy;
}

std::string fingerprint(const Feature& source, TopoShape face)
{
    if (face.isNull() || face.getShape().ShapeType() != TopAbs_FACE) {
        return {};
    }
    // Strip only the object's placement. Preserve the face's location within the solid.
    auto location = source.Shape.getShape().getShape().Location().Inverted();
    face.locate(location * face.getShape().Location());
    std::ostringstream stream;
    stream.imbue(std::locale::classic());
    face.exportBrep(stream);
    QCryptographicHash hash(QCryptographicHash::Sha256);
    hash.addData(source.getNameInDocument());
    hash.addData(QByteArray::fromStdString(stream.str()));
    return hash.result().toHex().toStdString();
}

void nameEditedFace(TopoShape& shape, int index, const std::string& identity)
{
    if (!shape.hasElementMap()) {
        shape.resetElementMap(std::make_shared<Data::ElementMap>());
    }
    const Data::IndexedName element("Face", index);
    const auto aliases = shape.getElementMappedNames(element);
    // Prefer the verified name for new links while keeping the kernel's history aliases.
    shape.setElementName(element, Data::MappedName("QFace-" + identity), shape.Tag, nullptr, true);
    for (const auto& [name, ids] : aliases) {
        shape.setElementName(element, name, shape.Tag, &ids);
    }
}

struct Bore
{
    gp_Ax2 start;
    double radius;
    double depth;
};

Bore describeBore(const TopoDS_Face& face)
{
    constexpr double fullTurn = 2 * std::numbers::pi;
    const BRepAdaptor_Surface surface(face);
    if (surface.GetType() != GeomAbs_Cylinder
        || std::abs(surface.LastUParameter() - surface.FirstUParameter() - fullTurn) > angularTolerance) {
        throw Base::ValueError("Select one complete cylindrical hole wall.");
    }
    const auto cylinder = surface.Cylinder();
    const auto axis = cylinder.Axis().Direction();
    const double first = surface.FirstVParameter();
    const double last = surface.LastVParameter();
    const double depth = last - first;
    const double radius = cylinder.Radius();
    GProp_GProps properties;
    BRepGProp::SurfaceProperties(face, properties);
    const double area = fullTurn * radius * depth;
    if (!std::isfinite(area) || depth <= holeTolerance || radius <= holeTolerance
        || std::abs(properties.Mass() - area) > holeTolerance * (depth + radius)) {
        throw Base::ValueError("Select one complete cylindrical hole wall.");
    }
    gp_Pnt point;
    gp_Vec du, dv;
    surface.D1((surface.FirstUParameter() + surface.LastUParameter()) / 2,
               (first + last) / 2, point, du, dv);
    auto normal = du.Crossed(dv);
    if (face.Orientation() == TopAbs_REVERSED) {
        normal.Reverse();
    }
    if (normal.Dot(gp_Vec(cylinder.Location(), point)) >= 0) {
        throw Base::ValueError("Select an internal hole wall.");
    }
    const auto start = cylinder.Location().Translated(gp_Vec(axis) * first);
    return {gp_Ax2(start, axis, cylinder.Position().XDirection()), radius, depth};
}

bool hasPlanarBoreEnds(const TopoDS_Shape& solid, const TopoDS_Face& wall, const Bore& bore)
{
    TopTools_IndexedDataMapOfShapeListOfShape facesByEdge;
    TopExp::MapShapesAndUniqueAncestors(solid, TopAbs_EDGE, TopAbs_FACE, facesByEdge);
    bool atStart = false;
    bool atEnd = false;
    for (TopExp_Explorer edges(wall, TopAbs_EDGE); edges.More(); edges.Next()) {
        const auto edge = TopoDS::Edge(edges.Current());
        if (BRep_Tool::IsClosed(edge, wall)) {
            continue;
        }
        const auto& adjacent = facesByEdge.FindFromKey(edge);
        if (adjacent.Extent() != 2) {
            return false;
        }
        const auto& other = adjacent.First().IsSame(wall) ? adjacent.Last() : adjacent.First();
        const BRepAdaptor_Surface neighbor(TopoDS::Face(other));
        const BRepAdaptor_Curve curve(edge);
        if (neighbor.GetType() != GeomAbs_Plane || curve.GetType() != GeomAbs_Circle
            || !neighbor.Plane().Axis().Direction().IsParallel(bore.start.Direction(), angularTolerance)) {
            return false;
        }
        const double axial = gp_Vec(bore.start.Location(), curve.Circle().Location())
                                 .Dot(gp_Vec(bore.start.Direction()));
        atStart |= std::abs(axial) <= holeTolerance;
        atEnd |= std::abs(axial - bore.depth) <= holeTolerance;
    }
    return atStart && atEnd;
}

int findResizedWall(const TopoShape& result, const Bore& original, double radius)
{
    const auto& shape = result.getShape();
    const gp_Lin axis(original.start.Axis());
    const auto center = original.start.Location().Translated(
        gp_Vec(original.start.Direction()) * (original.depth / 2)
    );
    int match = 0;
    for (TopExp_Explorer faces(shape, TopAbs_FACE); faces.More(); faces.Next()) {
        const auto& face = TopoDS::Face(faces.Current());
        const BRepAdaptor_Surface surface(face);
        if (surface.GetType() != GeomAbs_Cylinder
            || std::abs(surface.Cylinder().Radius() - radius) > holeTolerance
            || axis.Distance(surface.Cylinder().Location()) > holeTolerance
            || !surface.Cylinder().Axis().Direction().IsParallel(axis.Direction(), angularTolerance)) {
            continue;
        }
        GProp_GProps properties;
        BRepGProp::SurfaceProperties(face, properties);
        if (std::abs(properties.Mass() - 2 * std::numbers::pi * radius * original.depth)
                <= holeTolerance * (radius + original.depth)
            && properties.CentreOfMass().Distance(center) <= holeTolerance) {
            const auto resized = describeBore(face);
            if (match || std::abs(resized.depth - original.depth) > holeTolerance
                || !hasPlanarBoreEnds(shape, face, resized)) {
                return 0;
            }
            match = result.findShape(face);
        }
    }
    return match;
}

int findMovedFace(
    const TopoShape& result,
    const gp_Pnt& expectedCenter,
    const gp_Dir& normal,
    double area,
    double tolerance
)
{
    int match = 0;
    for (TopExp_Explorer faces(result.getShape(), TopAbs_FACE); faces.More(); faces.Next()) {
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
            if (match) {
                return 0;
            }
            match = result.findShape(face);
        }
    }
    return match;
}

}  // namespace

PROPERTY_SOURCE(Part::SvenPlanarOffset, Part::Feature)

SvenPlanarOffset::SvenPlanarOffset()
{
    ADD_PROPERTY_TYPE(Support, (nullptr), "Offset", App::Prop_None, "Source solid and selected face");
    ADD_PROPERTY_TYPE(Offset, (1.0), "Offset", App::Prop_None, "Signed distance from the source face");
    ADD_PROPERTY_TYPE(SupportFingerprint, (""), "Offset", App::Prop_Hidden,
                      "Identity of the selected source face");
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

TopoShape SvenPlanarOffset::calculate(const Feature& source, const char* faceName, double offset,
                                     int* editedFace)
{
    const double tolerance = 10 * Precision::Confusion();
    if (!std::isfinite(offset) || std::abs(offset) <= tolerance) {
        throw Base::ValueError("Enter a non-zero offset distance.");
    }

    TopoShape faceShape;
    auto solid = copySolidAndFace(source, faceName, faceShape);
    const auto& base = solid.getShape();
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
    const int match = findMovedFace(result, faceProperties.CentreOfMass().Translated(displacement),
                                   normal, area, tolerance);
    if (volumeChange * expectedVolumeChange <= 0
        || std::abs(volumeChange - expectedVolumeChange) > volumeTolerance
        || !match) {
        throw Base::ValueError("The offset intersects other geometry or crosses the opposite wall.");
    }
    if (editedFace) {
        *editedFace = match;
    }
    return result;
}

short SvenPlanarOffset::mustExecute() const
{
    return Support.isTouched() || Offset.isTouched() || SupportFingerprint.isTouched()
        || Feature::mustExecute();
}

void SvenPlanarOffset::setSupport(Feature& source, const std::string& faceName)
{
    const auto identity = fingerprint(source, source.Shape.getShape().getSubTopoShape(faceName.c_str(), true));
    if (identity.empty()) {
        throw Base::ValueError("The source face could not be resolved.");
    }
    Support.setValue(&source, std::vector<std::string>{faceName});
    SupportFingerprint.setValue(identity);
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
        const auto& shape = feature->Shape.getShape();
        auto faceName = faces.front();
        auto identity = fingerprint(*feature, shape.getSubTopoShape(faceName.c_str(), true));
        const std::string recorded = SupportFingerprint.getValue();
        if (!recorded.empty() && identity != recorded) {
            int match = 0;
            const int count = shape.countSubShapes(TopAbs_FACE);
            for (int index = 1; index <= count; ++index) {
                if (fingerprint(*feature, shape.getSubTopoShape(TopAbs_FACE, index)) != recorded) {
                    continue;
                }
                if (match) {
                    return new App::DocumentObjectExecReturn(
                        "The source face is ambiguous. Reselect it with Q."
                    );
                }
                match = index;
            }
            if (!match) {
                return new App::DocumentObjectExecReturn(
                    "The source face changed or disappeared. Reselect it with Q."
                );
            }
            faceName = "Face" + std::to_string(match);
            identity = recorded;
        }
        int editedFace = 0;
        auto result = buildResult(*feature, faceName.c_str(), editedFace);
        nameEditedFace(result, editedFace, identity);
        if (recorded.empty()) {
            SupportFingerprint.setValue(identity);
        }
        if (faceName != faces.front()) {
            Support.setValue(feature, std::vector<std::string>{faceName});
        }
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

TopoShape SvenPlanarOffset::buildResult(const Feature& source, const char* faceName, int& editedFace) const
{
    return calculate(source, faceName, Offset.getValue(), &editedFace);
}

PROPERTY_SOURCE(Part::SvenHoleResize, Part::SvenPlanarOffset)

SvenHoleResize::SvenHoleResize()
{
    ADD_PROPERTY_TYPE(Diameter, (1.0), "Hole", App::Prop_None, "New bore diameter");
    Offset.setStatus(App::Property::Hidden, true);
}

TopoShape SvenHoleResize::calculate(const Feature& source, const char* faceName, double diameter,
                                   int* editedFace)
{
    if (!std::isfinite(diameter) || diameter <= 2 * holeTolerance) {
        throw Base::ValueError("Enter a positive hole diameter.");
    }
    TopoShape face;
    auto solid = copySolidAndFace(source, faceName, face);
    const auto& base = solid.getShape();
    const auto& wall = TopoDS::Face(face.getShape());
    const auto bore = describeBore(wall);
    if (!hasPlanarBoreEnds(base, wall, bore)) {
        throw Base::ValueError("The hole wall must end at two perpendicular planar faces.");
    }

    const double radius = diameter / 2;
    const double change = radius - bore.radius;
    if (std::abs(change) <= holeTolerance) {
        throw Base::ValueError("Enter a diameter different from the current diameter.");
    }
    // Overlap the existing void or stock radially while keeping both axial limits exact.
    const double overlap = std::clamp(std::abs(change) / 10, 2 * holeTolerance, 1e-4);
    const double outerRadius = change > 0 ? radius : bore.radius + overlap;
    const double innerRadius = change > 0 ? std::max(bore.radius / 2, bore.radius - overlap) : radius;
    const TopoShape outer(BRepPrimAPI_MakeCylinder(bore.start, outerRadius, bore.depth).Shape());
    const TopoShape inner(BRepPrimAPI_MakeCylinder(bore.start, innerRadius, bore.depth).Shape());
    const auto ring = outer.makeElementCut(inner);
    const auto result = (change > 0 ? solid.makeElementCut(ring) : solid.makeElementFuse(ring))
                            .makeElementRefine();
    const auto& shape = result.getShape();
    const double expected = -std::numbers::pi * change * (radius + bore.radius) * bore.depth;
    const double tolerance = std::max(std::abs(expected) * 1e-7,
                                      2 * std::numbers::pi * bore.radius * bore.depth * holeTolerance);
    if (!isSingleSolid(shape) || !BRepCheck_Analyzer(shape).IsValid()
        || std::abs(volume(shape) - volume(base) - expected) > tolerance) {
        throw Base::ValueError("The new diameter would alter nearby geometry or open the side wall.");
    }
    const int match = findResizedWall(result, bore, radius);
    if (!match) {
        throw Base::ValueError("The new diameter would alter nearby geometry or open the side wall.");
    }
    if (editedFace) {
        *editedFace = match;
    }
    return result;
}

short SvenHoleResize::mustExecute() const
{
    return Diameter.isTouched() || SvenPlanarOffset::mustExecute();
}

TopoShape SvenHoleResize::buildResult(const Feature& source, const char* faceName, int& editedFace) const
{
    return calculate(source, faceName, Diameter.getValue(), &editedFace);
}
