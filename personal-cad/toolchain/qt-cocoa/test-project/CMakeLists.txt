cmake_minimum_required(VERSION 3.24)
project(PersonalCADQtAXRegression LANGUAGES CXX OBJCXX)

set(QT_SKIP_AUTO_PLUGIN_INCLUSION ON)
find_package(Qt6 6.11.2 EXACT REQUIRED COMPONENTS Gui Widgets Test)
find_library(APPKIT AppKit REQUIRED)
find_library(APPLICATION_SERVICES ApplicationServices REQUIRED)
find_library(SECURITY Security REQUIRED)

set(regression "${QTBASE_TEST_SOURCE}/tests/auto/other/qaccessibilitymac/tst_qaccessibilitymac.mm")
if(NOT EXISTS "${regression}")
    message(FATAL_ERROR "Set QTBASE_TEST_SOURCE to the verified patched Qt 6.11.2 tree.")
endif()

add_executable(tst_qaccessibilitymac "${regression}")
set_target_properties(tst_qaccessibilitymac PROPERTIES AUTOMOC ON)
target_link_libraries(tst_qaccessibilitymac PRIVATE
    Qt6::Gui Qt6::Widgets Qt6::Test "${APPKIT}" "${APPLICATION_SERVICES}" "${SECURITY}"
)
