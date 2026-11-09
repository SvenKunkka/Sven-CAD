// SPDX-License-Identifier: LGPL-2.1-or-later
/****************************************************************************
 *                                                                          *
 *   Copyright (c) 2023 Ondsel <development@ondsel.com>                     *
 *                                                                          *
 *   This file is part of FreeCAD.                                          *
 *                                                                          *
 *   FreeCAD is free software: you can redistribute it and/or modify it     *
 *   under the terms of the GNU Lesser General Public License as            *
 *   published by the Free Software Foundation, either version 2.1 of the   *
 *   License, or (at your option) any later version.                        *
 *                                                                          *
 *   FreeCAD is distributed in the hope that it will be useful, but         *
 *   WITHOUT ANY WARRANTY; without even the implied warranty of             *
 *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU       *
 *   Lesser General Public License for more details.                        *
 *                                                                          *
 *   You should have received a copy of the GNU Lesser General Public       *
 *   License along with FreeCAD. If not, see                                *
 *   <https://www.gnu.org/licenses/>.                                       *
 *                                                                          *
 ***************************************************************************/


#pragma once

#include <Mod/Assembly/AssemblyGlobal.h>

#ifdef FREECAD_PERSONAL_CAD
#include <App/DocumentObject.h>
#include <App/PropertyStandard.h>
#else
#include <Mod/Spreadsheet/App/Sheet.h>
#endif

namespace App
{
class DocumentObject;
}

namespace Assembly
{

class AssemblyObject;

#ifndef FREECAD_PERSONAL_CAD
class BomDataElement
{
public:
    BomDataElement(std::string objName, std::string columnName, std::string value)
        : objName(objName)
        , columnName(columnName)
        , value(value)
    {}
    ~BomDataElement()
    {}

    std::string objName;
    std::string columnName;
    std::string value;
};
#endif

#ifdef FREECAD_PERSONAL_CAD
class AssemblyExport BomObject: public App::DocumentObject
#else
class AssemblyExport BomObject: public Spreadsheet::Sheet
#endif
{
    PROPERTY_HEADER_WITH_OVERRIDE(Assembly::BomObject);

public:
    BomObject();
    ~BomObject() override;

#ifndef FREECAD_PERSONAL_CAD
    PyObject* getPyObject() override;
#endif

    const char* getViewProviderName() const override
    {
        return "AssemblyGui::ViewProviderBom";
    }

    App::DocumentObjectExecReturn* execute() override;

    void generateBOM();
    void addObjectToBom(App::DocumentObject* obj, size_t row, std::string index, bool isMirrored = false);
    void addObjectChildrenToBom(std::vector<App::DocumentObject*> objs, size_t& row, std::string index);
    bool isObjMirrored(App::DocumentObject* obj);

    AssemblyObject* getAssembly() const;

    bool hasQuantityColumn() const;
    int getColumnIndex(std::string name) const;
    std::string getText(size_t row, size_t col) const;

    App::PropertyStringList columnsNames;
    App::PropertyBool detailSubAssemblies;
    App::PropertyBool detailParts;
    App::PropertyBool onlyParts;

    std::vector<App::DocumentObject*> obj_list;
    std::vector<bool> obj_mirrored_list;

private:
    void setText(size_t row, size_t col, const std::string& value, bool forceText = false);
    std::string getBomPropertyValue(App::DocumentObject* obj, const std::string& baseName);

#ifdef FREECAD_PERSONAL_CAD
    App::PropertyStringList BomRows;
    std::vector<std::string> tableEntries;
#else
    void saveCustomColumnData();
    std::vector<BomDataElement> dataElements;
#endif
    std::string mirroredSuffix;
};


}  // namespace Assembly
