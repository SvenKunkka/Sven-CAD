// SPDX-License-Identifier: LGPL-2.1-or-later

#pragma once

#include <App/PropertyUnits.h>
#include <App/PropertyStandard.h>

#include "PartFeature.h"

namespace Part
{

class PartExport SvenPlanarOffset: public Feature
{
    PROPERTY_HEADER_WITH_OVERRIDE(Part::SvenPlanarOffset);

public:
    SvenPlanarOffset();

    App::PropertyLinkSub Support;
    App::PropertyDistance Offset;
    App::PropertyString SupportFingerprint;

    static bool supports(const App::DocumentObject* source);
    static TopoShape calculate(const Feature& source, const char* faceName, double offset,
                               int* editedFace = nullptr);
    void setSupport(Feature& source, const std::string& faceName);

    short mustExecute() const override;
    App::DocumentObjectExecReturn* execute() override;

protected:
    virtual TopoShape buildResult(const Feature& source, const char* faceName, int& editedFace) const;
};

class PartExport SvenHoleResize: public SvenPlanarOffset
{
    PROPERTY_HEADER_WITH_OVERRIDE(Part::SvenHoleResize);

public:
    SvenHoleResize();

    App::PropertyLength Diameter;

    static TopoShape calculate(const Feature& source, const char* faceName, double diameter,
                               int* editedFace = nullptr);
    short mustExecute() const override;

protected:
    TopoShape buildResult(const Feature& source, const char* faceName, int& editedFace) const override;
};

}  // namespace Part
