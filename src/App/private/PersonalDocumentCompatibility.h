// SPDX-License-Identifier: LGPL-2.1-or-later

#pragma once

#include <iosfwd>

namespace App
{

// Inspect the archive before restoring native objects or importing Python proxies.
void checkPersonalProject(std::istream& file, const char* filename);

}  // namespace App
