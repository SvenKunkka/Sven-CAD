// SPDX-License-Identifier: LGPL-2.1-or-later

#include "PersonalDocumentCompatibility.h"

#include <algorithm>
#include <array>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <string_view>

#include <Base/Exception.h>
#include <Base/Reader.h>
#include <zipios++/zipinputstream.h>

namespace
{

bool isRemovedObjectType(std::string_view type)
{
    constexpr std::array<std::string_view, 11> prefixes {
        "Spreadsheet::", "Mesh::", "MeshPart::", "Fem::", "Robot::", "Path::",
        "Points::", "Inspection::", "Reen::", "ReverseEngineering::", "BIM::"
    };
    return type == "Assembly::SimulationGroup" || type.starts_with("TechDraw::DrawViewSpreadsheet")
        || std::ranges::any_of(prefixes, [type](auto prefix) { return type.starts_with(prefix); });
}

bool isRemovedProxy(std::string_view module)
{
    return module == "CommandCreateSimulation" || module.starts_with("Arch")
        || module.starts_with("bim.") || module.starts_with("nativeifc.")
        || module.starts_with("Path.")
        || module.starts_with("OpenSCAD");
}

}  // namespace

void App::checkPersonalProject(std::istream& file, const char* filename)
{
    std::map<std::string, std::string> removedObjects;
    std::map<std::string, std::set<std::string>> dependencies;
    std::set<std::string> externalProjects;
    std::set<std::string> expressionObjects;

    try {
        zipios::ZipInputStream archive(file);
        Base::XMLReader reader(filename, archive);
        if (!reader.isValid()) {
            throw Base::FileException("Error reading compression file", filename);
        }
        reader.readElement("Document");
        if (std::string_view(reader.localName()) != "Document" || reader.level() != 1) {
            throw Base::FileException("Missing FCStd Document.xml root", filename);
        }
        if (reader.getAttribute<long>("SchemaVersion", 0) < 2) {
            throw Base::FileException("Unsupported FCStd document schema", filename);
        }

        std::string objectName;
        std::string dependencyOwner;
        while (!reader.isEndOfDocument()) {
            if (!reader.readNextElement()) {
                continue;
            }
            std::string_view tag(reader.localName());
            if (tag == "Object" || tag == "Feature") {
                objectName = reader.getAttribute<const char*>("name", "");
                std::string_view type(reader.getAttribute<const char*>("type", ""));
                if (!objectName.empty() && isRemovedObjectType(type)) {
                    removedObjects.try_emplace(objectName, type);
                }
            }
            else if (tag == "ObjectDeps") {
                dependencyOwner = reader.getAttribute<const char*>("Name", "");
            }
            else if (tag == "Dep" && !dependencyOwner.empty()) {
                dependencies[dependencyOwner].insert(reader.getAttribute<const char*>("Name", ""));
            }
            else if (tag == "Python" && !objectName.empty()) {
                std::string_view module(reader.getAttribute<const char*>("module", ""));
                if (isRemovedProxy(module)) {
                    removedObjects.try_emplace(objectName, module);
                }
            }
            else if (tag == "Property" && !objectName.empty()) {
                std::string_view type(reader.getAttribute<const char*>("type", ""));
                if (type == "Spreadsheet::PropertySheet") {
                    removedObjects.try_emplace(objectName, type);
                }
            }
            else if (tag == "Expression" && !objectName.empty()) {
                expressionObjects.insert(objectName);
            }
            else if (tag == "XLink") {
                std::string_view path(reader.getAttribute<const char*>("file", ""));
                if (!path.empty()) {
                    externalProjects.emplace(path);
                }
            }
        }
    }
    catch (const Base::XMLBaseException& error) {
        throw Base::FileException(error.what(), filename);
    }

    if (removedObjects.empty()) {
        return;
    }

    std::ostringstream message;
    message << "Personal CAD cannot open this document without losing unsupported objects.";
    for (const auto& [name, type] : removedObjects) {
        message << '\n' << name << " (" << type << ')';
    }
    for (const auto& [owner, targets] : dependencies) {
        if (removedObjects.contains(owner)) {
            continue;
        }
        for (const auto& target : targets) {
            if (removedObjects.contains(target)) {
                message << '\n' << owner << " depends on " << target;
                if (expressionObjects.contains(owner)) {
                    message << " and has expressions";
                }
            }
        }
    }
    for (const auto& project : externalProjects) {
        message << "\nExternal project: " << project;
    }
    message << "\nOpen the original in FreeCAD to inspect these objects or create a compatible copy.";
    throw Base::FileException(message.str(), filename);
}
