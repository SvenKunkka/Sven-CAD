// SPDX-License-Identifier: LGPL-2.1-or-later

#pragma once

#include <gp_Ax3.hxx>

#include <App/PropertyLinks.h>
#include <App/PropertyUnits.h>
#include <Mod/Part/App/PartFeature.h>
#include <Mod/PartDesign/PartDesignGlobal.h>

namespace PartDesign
{

class PartDesignExport ExternalThread: public Part::Feature
{
    PROPERTY_HEADER_WITH_OVERRIDE(PartDesign::ExternalThread);

public:
    ExternalThread();

    App::PropertyLinkSub Support;
    App::PropertyEnumeration ThreadSize;
    App::PropertyLength ThreadLength;
    App::PropertyBool LeftHanded;
    App::PropertyBool Modeled;
    App::PropertyLength Pitch;
    App::PropertyLength MajorDiameter;
    App::PropertyLength MinorDiameter;

    struct Shaft
    {
        Part::TopoShape shape;
        gp_Ax3 axes;
        double length;
        double radius;
        int size;
    };

    static const char* Sizes[];
    static constexpr int MaxModeledTurns = 64;
    static Shaft describe(const Part::Feature& source, const char* faceName);
    static double pitchForSize(int size);
    static Part::TopoShape makeShape(
        const Shaft& shaft,
        int size,
        double length,
        bool leftHanded,
        bool modeled
    );
    void setThreadResult(Part::Feature& source, const Part::TopoShape& shape);

    App::DocumentObjectExecReturn* execute() override;
    short mustExecute() const override;
};

}  // namespace PartDesign
