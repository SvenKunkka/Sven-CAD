cmake_minimum_required(VERSION 3.24)
project(PersonalCADQtCocoa VERSION 6.11.2 LANGUAGES C CXX OBJCXX)

if(NOT APPLE OR NOT CMAKE_OSX_ARCHITECTURES STREQUAL "arm64")
    message(FATAL_ERROR "The personal CAD Cocoa comparison is for macOS arm64.")
endif()
if(NOT IS_DIRECTORY "${QTBASE_SOURCE}/src/plugins/platforms/cocoa")
    message(FATAL_ERROR "Set QTBASE_SOURCE to the verified Qt 6.11.2 source tree.")
endif()

set(QT_SKIP_AUTO_PLUGIN_INCLUSION ON)
find_package(Qt6 6.11.2 EXACT REQUIRED COMPONENTS Core CorePrivate Gui GuiPrivate)
find_package(Qt6BuildInternals 6.11.2 EXACT REQUIRED)
qt_internal_setup_default_target_function_options()

set(CMAKE_AUTOMOC ON)
set(QT_BUILD_DIR "${CMAKE_BINARY_DIR}")
set(QT_CMAKE_DIR "${QTBASE_SOURCE}/cmake")
set(INSTALL_PLUGINSDIR plugins)

get_target_property(gui_features Qt6::Gui QT_ENABLED_PUBLIC_FEATURES)
foreach(feature IN ITEMS accessibility opengl sessionmanager vulkan)
    if(feature IN_LIST gui_features)
        set(QT_FEATURE_${feature} ON)
    else()
        message(FATAL_ERROR "The pinned Qt Gui must enable ${feature} for this comparison.")
    endif()
endforeach()

foreach(framework IN ITEMS Foundation AppKit Carbon CoreServices CoreVideo
                           IOKit IOSurface Metal QuartzCore UniformTypeIdentifiers)
    find_library(FW${framework} NAMES "${framework}" REQUIRED)
endforeach()

add_subdirectory("${QTBASE_SOURCE}/src/plugins/platforms/cocoa" upstream-cocoa)
find_path(MOLTENVK_INCLUDE_DIR NAMES MoltenVK/mvk_vulkan.h REQUIRED)
target_include_directories(QCocoaIntegrationPlugin PRIVATE "${MOLTENVK_INCLUDE_DIR}")
