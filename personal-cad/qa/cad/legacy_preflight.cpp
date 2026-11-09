// SPDX-License-Identifier: LGPL-2.1-or-later

#include <fstream>
#include <iostream>

#include <xercesc/util/PlatformUtils.hpp>

#include <Base/Exception.h>
#include "App/private/PersonalDocumentCompatibility.h"

int main(int argc, char* argv[])
{
    if (argc != 2) {
        return 64;
    }
    std::ifstream file(argv[1], std::ios::binary);
    if (!file) {
        return 66;
    }
    // FreeCADBase's static transcoders are destroyed at process exit.
    xercesc::XMLPlatformUtils::Initialize();
    int result = 0;
    try {
        App::checkPersonalProject(file, argv[1]);
    }
    catch (const Base::Exception& error) {
        std::cerr << error.what() << '\n';
        result = 2;
    }
    return result;
}
