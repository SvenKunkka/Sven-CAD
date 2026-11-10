// SPDX-License-Identifier: LGPL-2.1-or-later

#pragma once

#include <algorithm>
#include <format>
#include <string>
#include <string_view>

namespace PartDesign
{

struct MetricCoarseThreadLimits
{
    struct Range
    {
        double min;
        double max;
    };

    std::string_view size;
    Range normalEngagement;
    Range externalMajor;
    Range externalPitch;
    double externalMinRootRadius;
    Range internalMinor;
    Range internalPitch;

    // Normal engagement ranges and diameter limits in mm. Bossard F.089,
    // Metric ISO Threads, coarse-thread tables for external 6g and internal 6H.
    static const MetricCoarseThreadLimits* find(std::string_view size)
    {
        static constexpr MetricCoarseThreadLimits data[] = {
            {"M2x0.4",   {1.0, 3.0}, {1.886, 1.981}, {1.654, 1.721}, .050, {1.567, 1.679}, {1.740, 1.830}},
            {"M2.5x0.45",{1.3, 3.8}, {2.380, 2.480}, {2.117, 2.188}, .056, {2.013, 2.138}, {2.208, 2.303}},
            {"M3x0.5",   {1.5, 4.5}, {2.874, 2.980}, {2.580, 2.655}, .063, {2.459, 2.599}, {2.675, 2.775}},
            {"M4x0.7",   {2.0, 6.0}, {3.838, 3.978}, {3.433, 3.523}, .088, {3.242, 3.422}, {3.545, 3.663}},
        };
        const auto it = std::ranges::find(data, size, &MetricCoarseThreadLimits::size);
        return it == std::end(data) ? nullptr : it;
    }

    std::string externalReference() const
    {
        return std::format("6g | d {:.3f}–{:.3f} mm | d2 {:.3f}–{:.3f} mm | r ≥ {:.3f} mm | L {:.1f}–{:.1f} mm",
                           externalMajor.min, externalMajor.max, externalPitch.min, externalPitch.max,
                           externalMinRootRadius, normalEngagement.min, normalEngagement.max);
    }

    std::string internalReference() const
    {
        return std::format("6H | D1 {:.3f}–{:.3f} mm | D2 {:.3f}–{:.3f} mm | L {:.1f}–{:.1f} mm",
                           internalMinor.min, internalMinor.max, internalPitch.min, internalPitch.max,
                           normalEngagement.min, normalEngagement.max);
    }
};

}  // namespace PartDesign
