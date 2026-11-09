# SPDX-License-Identifier: LGPL-2.1-or-later

set(Assembly_LIBS
    Part
    PartDesign
    FreeCADApp
    OndselSolver
)

if(FREECAD_PERSONAL_CAD)
    # Omit the motion methods from the Python type itself in this edition.
    set(AssemblyObjectStub "${CMAKE_CURRENT_SOURCE_DIR}/AssemblyObject.pyi")
    # The binding generator derives the module name from the src/Mod path.
    set(PersonalAssemblyObjectStub "${CMAKE_BINARY_DIR}/src/Mod/Assembly/App/AssemblyObject.pyi")
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${AssemblyObjectStub}")
    file(READ "${AssemblyObjectStub}" AssemblyObjectStubContents)
    string(REGEX REPLACE "    # BEGIN MOTION SIMULATION API\n[^#]*    # END MOTION SIMULATION API\n"
        "" PersonalAssemblyObjectStubContents "${AssemblyObjectStubContents}")
    if(PersonalAssemblyObjectStubContents STREQUAL AssemblyObjectStubContents)
        message(FATAL_ERROR "Unable to remove motion simulation methods from AssemblyObject.pyi")
    endif()
    file(CONFIGURE OUTPUT "${PersonalAssemblyObjectStub}" CONTENT "${PersonalAssemblyObjectStubContents}" @ONLY)

    set(AssemblyBindingsDir "${CMAKE_SOURCE_DIR}/src/Tools/bindings")
    set(AssemblyObjectBindingsHeader "${CMAKE_CURRENT_BINARY_DIR}/AssemblyObjectPy.h")
    set(AssemblyObjectBindingsSource "${CMAKE_CURRENT_BINARY_DIR}/AssemblyObjectPy.cpp")
    add_custom_command(
        OUTPUT "${AssemblyObjectBindingsHeader}" "${AssemblyObjectBindingsSource}"
        COMMAND "${Python3_EXECUTABLE}" "${AssemblyBindingsDir}/generate.py"
            --outputPath "${CMAKE_CURRENT_BINARY_DIR}" "${PersonalAssemblyObjectStub}"
        MAIN_DEPENDENCY "${PersonalAssemblyObjectStub}"
        DEPENDS "${AssemblyBindingsDir}/generate.py" "${AssemblyBindingsDir}/templates/templateClassPyExport.py"
        VERBATIM
    )
    add_custom_target(AssemblyObjectPythonBindings
        DEPENDS "${AssemblyObjectBindingsHeader}" "${AssemblyObjectBindingsSource}"
    )
else()
    list(APPEND Assembly_LIBS Spreadsheet)
    generate_from_py(AssemblyObject)
    generate_from_py(BomObject)
    generate_from_py(SimulationGroup)
endif()
generate_from_py(AssemblyLink)
generate_from_py(BomGroup)
generate_from_py(JointGroup)
generate_from_py(ViewGroup)
generate_from_py(SnapshotGroup)


SET(Python_SRCS
    AssemblyObject.pyi
    AssemblyObjectPyImp.cpp
    AssemblyLink.pyi
    AssemblyLinkPyImp.cpp
    BomGroup.pyi
    BomGroupPyImp.cpp
    JointGroup.pyi
    JointGroupPyImp.cpp
    ViewGroup.pyi
    ViewGroupPyImp.cpp
    SnapshotGroup.pyi
    SnapshotGroupPyImp.cpp
)
if(NOT FREECAD_PERSONAL_CAD)
    list(APPEND Python_SRCS BomObject.pyi BomObjectPyImp.cpp SimulationGroup.pyi SimulationGroupPyImp.cpp)
endif()
SOURCE_GROUP("Python" FILES ${Python_SRCS})

SET(Module_SRCS
    AppAssembly.cpp
    AppAssemblyPy.cpp
    PreCompiled.h
)
SOURCE_GROUP("Module" FILES ${Module_SRCS})

SET(Assembly_SRCS
    AssemblyObject.cpp
    AssemblyObject.h
    AssemblyLink.cpp
    AssemblyLink.h
    AssemblyUtils.cpp
    AssemblyUtils.h
    BomObject.cpp
    BomObject.h
    Groups.cpp
    Groups.h
    ${Module_SRCS}
    ${Python_SRCS}
)

add_library(Assembly SHARED ${Assembly_SRCS})
if(FREECAD_PERSONAL_CAD)
    add_dependencies(Assembly AssemblyObjectPythonBindings)
endif()
target_include_directories(
    Assembly
    PRIVATE
    ${CMAKE_BINARY_DIR}
    ${CMAKE_SOURCE_DIR}/src
    ${CMAKE_BINARY_DIR}/src
    ${CMAKE_CURRENT_BINARY_DIR}
)
target_link_libraries(Assembly ${Assembly_LIBS})
target_compile_warn_error(Assembly)

if(FREECAD_USE_PCH)
    target_precompile_headers(Assembly PRIVATE
            $<$<COMPILE_LANGUAGE:CXX>:"${CMAKE_CURRENT_LIST_DIR}/PreCompiled.h">
    )
endif(FREECAD_USE_PCH)

SET_BIN_DIR(Assembly AssemblyApp)
SET_PYTHON_PREFIX_SUFFIX(Assembly)

INSTALL(TARGETS Assembly DESTINATION ${CMAKE_INSTALL_LIBDIR})
