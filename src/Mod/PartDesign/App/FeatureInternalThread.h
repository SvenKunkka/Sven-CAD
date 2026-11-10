// SPDX-License-Identifier: LGPL-2.1-or-later

#pragma once

#include <Mod/Part/App/FeatureSvenPlanarOffset.h>
#include <Mod/PartDesign/PartDesignGlobal.h>

namespace PartDesign
{

class PartDesignExport InternalThread: public Part::Feature
{
    PROPERTY_HEADER_WITH_OVERRIDE(PartDesign::InternalThread);

public:
    InternalThread();

    App::PropertyLinkSub Support;
    App::PropertyEnumeration ThreadSize;
    App::PropertyLength ThreadLength;
    App::PropertyBool LeftHanded;
    App::PropertyBool Modeled;
    App::PropertyBool ReverseStart;
    App::PropertyLength Pitch;
    App::PropertyLength MajorDiameter;
    App::PropertyLength BoreDiameter;
    App::PropertyString ManufacturingClass;
    App::PropertyString ManufacturingLimits;

    struct HoleWall : Part::SvenHoleResize::Bore
    {
        int size;
    };

    static HoleWall describe(const Part::Feature& source, const char* faceName);
    static Part::TopoShape makeShape(const HoleWall& bore, int size, double length, bool leftHanded,
                                   bool modeled, bool reverseStart);
    void setThreadResult(Part::Feature& source, const HoleWall& bore, const Part::TopoShape& shape);
    short mustExecute() const override;
    App::DocumentObjectExecReturn* execute() override;
};

}  // namespace PartDesign
