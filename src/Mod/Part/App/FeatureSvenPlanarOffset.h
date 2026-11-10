// SPDX-License-Identifier: LGPL-2.1-or-later

#pragma once

#include <App/PropertyUnits.h>

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

    static bool supports(const App::DocumentObject* source);
    static TopoShape calculate(const Feature& source, const char* faceName, double offset);

    short mustExecute() const override;
    App::DocumentObjectExecReturn* execute() override;
};

}  // namespace Part
