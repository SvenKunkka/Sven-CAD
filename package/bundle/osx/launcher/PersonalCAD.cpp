// SPDX-License-Identifier: LGPL-2.1-or-later

#include <mach-o/dyld.h>
#include <unistd.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <vector>

int main(int, char** argv)
{
    uint32_t executablePathSize = 0;
    _NSGetExecutablePath(nullptr, &executablePathSize);
    std::vector<char> executablePath(executablePathSize);
    if (_NSGetExecutablePath(executablePath.data(), &executablePathSize) != 0) {
        std::fputs("PersonalCAD could not locate the bundle launcher.\n", stderr);
        return EXIT_FAILURE;
    }

    std::error_code error;
    const auto launcher = std::filesystem::canonical(executablePath.data(), error);
    if (error) {
        std::fprintf(stderr, "PersonalCAD could not locate the bundle: %s\n", error.message().c_str());
        return EXIT_FAILURE;
    }

    const auto resources = launcher.parent_path().parent_path() / "Resources";
    const auto app = resources / "bin" / "FreeCAD";
    const auto plugins = resources / "lib" / "qt6" / "plugins";
    const auto fonts = resources / "etc" / "fonts";
    const auto certificates = resources / "ssl" / "cacert.pem";

    if (setenv("PYTHONHOME", resources.c_str(), 1) != 0
        || setenv("PYTHONPATH", resources.c_str(), 1) != 0
        || setenv("PYTHONNOUSERSITE", "1", 1) != 0
        || setenv("QT_PLUGIN_PATH", plugins.c_str(), 1) != 0
        || setenv("QT_QPA_PLATFORM_PLUGIN_PATH", (plugins / "platforms").c_str(), 1) != 0
        || setenv("FONTCONFIG_FILE", (fonts / "fonts.conf").c_str(), 1) != 0
        || setenv("FONTCONFIG_PATH", fonts.c_str(), 1) != 0
        || setenv("SSL_CERT_FILE", certificates.c_str(), 1) != 0) {
        std::perror("PersonalCAD could not prepare the bundle environment");
        return EXIT_FAILURE;
    }

    std::string appPath = app.string();
    argv[0] = appPath.data();
    execv(appPath.c_str(), argv);
    std::perror("PersonalCAD could not launch its executable");
    return EXIT_FAILURE;
}
