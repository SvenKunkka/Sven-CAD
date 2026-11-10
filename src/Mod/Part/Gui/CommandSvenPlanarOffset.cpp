// SPDX-License-Identifier: LGPL-2.1-or-later

#include <algorithm>
#include <cmath>
#include <exception>

#include <QCoreApplication>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QFormLayout>
#include <QLabel>
#include <QPushButton>
#include <QScopeGuard>
#include <QTimer>

#include <BRepAdaptor_Surface.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Lin.hxx>
#include <gp_Pln.hxx>
#include <Precision.hxx>
#include <Standard_Failure.hxx>
#include <TopoDS.hxx>

#include <App/AutoTransaction.h>
#include <App/Document.h>
#include <App/DocumentObserver.h>
#include <App/GeoFeatureGroupExtension.h>
#include <App/MappedElement.h>
#include <App/Part.h>
#include <Base/Exception.h>
#include <Gui/Application.h>
#include <Gui/Command.h>
#include <Gui/Control.h>
#include <Gui/Document.h>
#include <Gui/MainWindow.h>
#include <Gui/Selection/Selection.h>
#include <Mod/Part/App/FeatureSvenPlanarOffset.h>

#include "SoBrepEdgeSet.h"
#include "ViewProviderExt.h"
#include "ViewProviderPreviewExtension.h"

namespace
{

using Part::SvenPlanarOffset;
using Part::SvenHoleResize;

Part::Feature* selectedSource(const App::SubObjectT& selection)
{
    auto* source = selection.getSubObject();
    if (!SvenPlanarOffset::supports(source)) {
        return nullptr;
    }
    int faceIndex = 0;
    if (selection.getOldElementName(&faceIndex) != "Face" || faceIndex < 1) {
        return nullptr;
    }
    // Validate the original selection path, including wrappers which would be lost by resolving links.
    auto path = selection.getSubObjectList();
    if (path.empty() || path.back() != source) {
        return nullptr;
    }
    for (size_t i = 0; i + 1 < path.size(); ++i) {
        if (path[i]->getTypeId() != App::Part::getClassTypeId()
            || App::GeoFeatureGroupExtension::getGroupOfObject(path[i + 1]) != path[i]) {
            return nullptr;
        }
    }
    return static_cast<Part::Feature*>(source);
}

GProp_GProps faceProperties(const TopoDS_Face& face)
{
    GProp_GProps properties;
    BRepGProp::SurfaceProperties(face, properties);
    return properties;
}

gp_Dir orientedPlaneNormal(const TopoDS_Face& face, const gp_Pln& plane)
{
    auto normal = plane.Axis().Direction();
    if (face.Orientation() == TopAbs_REVERSED) {
        normal.Reverse();
    }
    return normal;
}

bool coaxial(const gp_Cylinder& cylinder, const gp_Cylinder& other)
{
    return cylinder.Axis().Direction().IsParallel(other.Axis().Direction(), 1e-6)
        && gp_Lin(cylinder.Axis()).Distance(other.Location()) <= 10 * Precision::Confusion();
}

bool sameSurface(const BRepAdaptor_Surface& first, const BRepAdaptor_Surface& second)
{
    const double tolerance = 10 * Precision::Confusion();
    const double angle = 1e-6;
    if (first.GetType() != second.GetType()) {
        return false;
    }
    if (first.GetType() == GeomAbs_Plane) {
        const auto plane = first.Plane();
        const auto other = second.Plane();
        return plane.Axis().Direction().IsParallel(other.Axis().Direction(), angle)
            && plane.Distance(other.Location()) <= tolerance;
    }
    if (first.GetType() == GeomAbs_Cylinder) {
        const auto cylinder = first.Cylinder();
        const auto other = second.Cylinder();
        return coaxial(cylinder, other)
            && std::abs(cylinder.Radius() - other.Radius()) <= tolerance;
    }
    return false;
}

void copyAppearance(
    Part::Feature& source,
    SvenPlanarOffset& result,
    const char* supportFace,
    PartGui::ViewProviderPartExt& sourceView,
    PartGui::ViewProviderPartExt& resultView
)
{
    const auto& materials = sourceView.ShapeAppearance.getValues();
    if (materials.empty()) {
        return;
    }
    if (materials.size() == 1) {
        resultView.ShapeAppearance.setValues(materials);
        return;
    }

    const auto& originalShape = source.Shape.getShape();
    const auto& resultShape = result.Shape.getShape();
    const auto sourceFaces = originalShape.countSubShapes(TopAbs_FACE);
    const auto resultFaces = resultShape.countSubShapes(TopAbs_FACE);
    const size_t coloredFaces = std::min<size_t>(sourceFaces, materials.size());
    const auto supportShape = originalShape.getSubTopoShape(supportFace, true);
    if (supportShape.isNull() || supportShape.getShape().ShapeType() != TopAbs_FACE) {
        return;
    }
    const auto& support = TopoDS::Face(supportShape.getShape());
    const int selectedIndex = originalShape.findShape(support);
    const BRepAdaptor_Surface supportSurface(support);
    if (selectedIndex <= 0 || static_cast<size_t>(selectedIndex) > coloredFaces) {
        return;
    }
    std::vector<int> mapped(resultFaces, -1);
    for (size_t i = 0; i < coloredFaces; ++i) {
        const auto name = "Face" + std::to_string(i + 1);
        for (const auto& face : Part::Feature::getElementFromSource(&result, "", &source, name.c_str())) {
            const auto index = face.index.getIndex();
            if (index > 0 && static_cast<size_t>(index) <= mapped.size()) {
                auto& previous = mapped[index - 1];
                if (previous == -1) {
                    previous = static_cast<int>(i);
                }
                else if (previous >= 0 && materials[previous] != materials[i]) {
                    previous = -2;
                }
            }
        }
    }

    const auto* resize = freecad_cast<SvenHoleResize*>(&result);
    const auto normal = resize ? gp_Dir() : orientedPlaneNormal(support, supportSurface.Plane());
    const auto cap = faceProperties(support);
    const auto expectedCap = cap.CentreOfMass().Translated(gp_Vec(normal) * result.Offset.getValue());
    const double tolerance = 10 * Precision::Confusion();
    std::vector<App::Material> appearance(resultFaces, materials.front());
    for (size_t i = 0; i < appearance.size(); ++i) {
        const auto face = TopoDS::Face(resultShape.getSubShape(TopAbs_FACE, static_cast<int>(i + 1)));
        const BRepAdaptor_Surface surface(face);
        const auto props = faceProperties(face);
        const bool resizedWall = resize && surface.GetType() == GeomAbs_Cylinder
            && coaxial(supportSurface.Cylinder(), surface.Cylinder())
            && std::abs(surface.Cylinder().Radius() - resize->Diameter.getValue() / 2) <= tolerance
            && props.CentreOfMass().Distance(cap.CentreOfMass()) <= tolerance;
        if (resizedWall || (!resize && surface.GetType() == GeomAbs_Plane
            && orientedPlaneNormal(face, surface.Plane()).IsEqual(normal, 1e-6)
            && props.CentreOfMass().Distance(expectedCap) <= tolerance
            && std::abs(props.Mass() - cap.Mass()) <= tolerance * std::sqrt(cap.Mass()))) {
            appearance[i] = materials[selectedIndex - 1];
        }
        else if (mapped[i] >= 0) {
            appearance[i] = materials[mapped[i]];
        }
        else if (surface.GetType() == GeomAbs_Plane || surface.GetType() == GeomAbs_Cylinder) {
            // Imported faces may have no recoverable history. Use a coincident surface
            // only when its matching source faces agree on the appearance.
            int match = -1;
            for (size_t j = 0; j < coloredFaces; ++j) {
                const auto original = TopoDS::Face(
                    originalShape.getSubShape(TopAbs_FACE, static_cast<int>(j + 1))
                );
                if (!sameSurface(surface, BRepAdaptor_Surface(original))) {
                    continue;
                }
                if (match >= 0 && materials[match] != materials[j]) {
                    match = -1;
                    break;
                }
                match = static_cast<int>(j);
            }
            if (match >= 0) {
                appearance[i] = materials[match];
            }
        }
    }
    resultView.ShapeAppearance.setValues(appearance);
}

class CmdSvenPlanarOffset: public Gui::Command
{
    Q_DECLARE_TR_FUNCTIONS(CmdSvenPlanarOffset)

public:
    CmdSvenPlanarOffset()
        : Command("Part_SvenPlanarOffset")
    {
        sAppModule = "Part";
        sGroup = QT_TR_NOOP("Part");
        sMenuText = QT_TR_NOOP("Offset Planar Face");
        sToolTipText = QT_TR_NOOP("Offsets a plane or resizes a cylindrical hole on a STEP solid");
        sStatusTip = sToolTipText;
        sPixmap = "Part_Offset";
        eType = ForEdit;
    }

    const char* className() const override
    {
        return "CmdSvenPlanarOffset";
    }

protected:
    bool isActive() override
    {
        return hasActiveDocument() && !Gui::Control().activeDialog() && !getActiveGuiDocument()->getInEdit();
    }

    void activated(int) override
    {
        auto selection = Gui::Selection().getSelectionT(nullptr, Gui::ResolveMode::NoResolve);
        const App::SubObjectT* selectedFace = nullptr;
        SvenPlanarOffset* existing = nullptr;
        if (selection.size() <= 2) {
            for (const auto& entry : selection) {
                if (selectedSource(entry)) {
                    selectedFace = &entry;
                }
                else if (!entry.hasSubElement()) {
                    existing = freecad_cast<SvenPlanarOffset*>(entry.getSubObject());
                }
            }
        }
        auto* source = selectedFace ? selectedSource(*selectedFace) : nullptr;
        if (selection.size() == 2 && (!existing || existing->Support.getValue() != source)) {
            Gui::getMainWindow()->showMessage(
                tr("Select the Q edit and a replacement face on its original solid."), 5000
            );
            return;
        }
        if (!source) {
            Gui::getMainWindow()->showMessage(tr("Select a planar face or cylindrical hole wall on a STEP solid."), 5000);
            return;
        }
        auto* view = freecad_cast<PartGui::ViewProviderPartExt*>(
            getActiveGuiDocument()->getViewProvider(source)
        );
        if (!view || !view->isShow()) {
            Gui::getMainWindow()->showMessage(tr("Show the source solid before editing its face."), 5000);
            return;
        }
        const auto& reference = *selectedFace;
        const App::DocumentObjectT edit(existing);
        auto face = reference.getNewElementName();
        if (face.empty()) {
            face = reference.getElementName();
        }

        double currentDiameter = 0;
        const auto selected = source->Shape.getShape().getSubTopoShape(face.c_str(), true);
        if (!selected.isNull() && selected.getShape().ShapeType() == TopAbs_FACE) {
            const BRepAdaptor_Surface surface(TopoDS::Face(selected.getShape()));
            if (surface.GetType() == GeomAbs_Cylinder) {
                currentDiameter = 2 * surface.Cylinder().Radius();
            }
        }
        const bool resize = currentDiameter > 0;
        auto* existingResize = freecad_cast<SvenHoleResize*>(existing);
        if (existing && resize != (existingResize != nullptr)) {
            Gui::getMainWindow()->showMessage(tr("Select a replacement face of the same type."), 5000);
            return;
        }
        const auto title = existing ? tr("Reselect Q Support")
            : resize ? tr("Resize Cylindrical Hole") : tr("Offset Planar Face");
        QDialog dialog(Gui::getMainWindow());
        dialog.setObjectName(QStringLiteral("SvenPlanarOffsetDialog"));
        dialog.setWindowTitle(title);
        auto* layout = new QFormLayout(&dialog);
        auto* distance = new QDoubleSpinBox(&dialog);
        distance->setObjectName(QStringLiteral("SvenPlanarOffsetDistance"));
        distance->setDecimals(4);
        distance->setRange(resize ? 0.0001 : -1000000.0, 1000000.0);
        distance->setSingleStep(0.1);
        distance->setSuffix(QStringLiteral(" mm"));
        distance->setValue(resize ? currentDiameter : 1);
        if (resize) {
            auto* existing = new QLabel(distance->text(), &dialog);
            layout->addRow(tr("Current diameter"), existing);
        }
        if (existing) {
            distance->setValue(existingResize ? existingResize->Diameter.getValue()
                                              : existing->Offset.getValue());
        }
        layout->addRow(resize ? tr("New diameter") : tr("Distance"), distance);
        auto* status = new QLabel(&dialog);
        status->setObjectName(QStringLiteral("SvenPlanarOffsetStatus"));
        status->setMinimumWidth(300);
        status->setWordWrap(true);
        layout->addRow(status);
        auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
        layout->addRow(buttons);
        QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, [&] {
            if (distance->hasAcceptableInput()) {
                dialog.accept();
            }
        });
        QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);

        Gui::CoinPtr<SoSeparator> annotation(view->getOrCreateAnnotation());
        Gui::CoinPtr<PartGui::SoPreviewShape> preview(new PartGui::SoPreviewShape);
        preview->setName("SvenPlanarOffsetPreview");
        const auto removePreview = [&] {
            if (annotation->findChild(preview) >= 0) {
                annotation->removeChild(preview);
            }
        };
        auto cleanup = qScopeGuard(removePreview);
        const auto updatePreview = [&] {
            removePreview();
            auto* okay = buttons->button(QDialogButtonBox::Ok);
            okay->setEnabled(false);
            try {
                auto* support = selectedSource(reference);
                if (!support) {
                    status->setText(tr("The source face is no longer available."));
                    return;
                }
                auto result = resize
                    ? SvenHoleResize::calculate(*support, face.c_str(), distance->value())
                    : SvenPlanarOffset::calculate(*support, face.c_str(), distance->value());
                PartGui::ViewProviderPartExt::setupCoinGeometry(
                    result.getShape(),
                    preview,
                    0.5,
                    15.0
                );
                int lines = 1;
                for (int i = 0; i < preview->lineset->coordIndex.getNum(); ++i) {
                    if (preview->lineset->coordIndex[i] < 0) {
                        ++lines;
                    }
                }
                preview->lineset->materialIndex.setNum(lines);
                for (int i = 0; i < lines; ++i) {
                    preview->lineset->materialIndex.set1Value(i, 0);
                }
                annotation->addChild(preview);
                status->setText(resize ? tr("The hole axis and depth are preserved.")
                                       : tr("Positive moves outward. Negative moves inward."));
                okay->setEnabled(true);
            }
            catch (const Base::Exception& error) {
                status->setText(QCoreApplication::translate("SvenPlanarOffset", error.what()));
            }
            catch (const Standard_Failure&) {
                status->setText(tr("The geometry engine could not preview this edit."));
            }
            catch (const std::exception&) {
                status->setText(tr("The geometry engine could not preview this edit."));
            }
        };
        QTimer debounce(&dialog);
        debounce.setSingleShot(true);
        debounce.setInterval(150);
        QObject::connect(&debounce, &QTimer::timeout, &dialog, updatePreview);
        QObject::connect(distance, qOverload<double>(&QDoubleSpinBox::valueChanged), &dialog, [&] {
            removePreview();
            buttons->button(QDialogButtonBox::Ok)->setEnabled(false);
            debounce.start();
        });
        updatePreview();
        distance->selectAll();
        if (dialog.exec() != QDialog::Accepted || !buttons->button(QDialogButtonBox::Ok)->isEnabled()) {
            return;
        }
        removePreview();
        commit(reference, face, distance->value(), resize, title, edit, existing != nullptr);
    }

private:
    void commit(const App::SubObjectT& reference, const std::string& face, double distance,
                bool resize, const QString& title, const App::DocumentObjectT& edit, bool rebind)
    {
        auto* source = selectedSource(reference);
        auto* document = getActiveDocument();
        auto* gui = getActiveGuiDocument();
        auto* existing = freecad_cast<SvenPlanarOffset*>(edit.getObject());
        if (!source || !gui || source->getDocument() != document || document->hasPendingTransaction()
            || document->getBookedTransactionID() != App::NullTransaction
            || (rebind && (!existing || existing->Support.getValue() != source))) {
            Gui::getMainWindow()->showMessage(tr("The source or document changed before confirmation."), 5000);
            return;
        }
        const int transactionId = document->openTransaction(title.toStdString());
        if (transactionId == App::NullTransaction) {
            Gui::getMainWindow()->showMessage(tr("The face edit could not be applied."), 5000);
            return;
        }
        App::AutoTransaction transaction(transactionId);
        try {
            auto* result = existing ? existing : resize
                ? static_cast<SvenPlanarOffset*>(document->addObject<SvenHoleResize>("HoleResize"))
                : document->addObject<SvenPlanarOffset>("PlanarOffset");
            if (!result) {
                throw Base::ValueError("The face edit could not be created.");
            }
            if (!existing) {
                if (auto* parent = App::GeoFeatureGroupExtension::getGroupOfObject(source)) {
                    static_cast<App::Part*>(parent)->addObject(result);
                }
            }
            result->setSupport(*source, face);
            if (resize) {
                static_cast<SvenHoleResize*>(result)->Diameter.setValue(distance);
            }
            else {
                result->Offset.setValue(distance);
            }
            if (!result->recomputeFeature()) {
                throw Base::ValueError("The face edit could not be applied.");
            }
            auto* sourceView = freecad_cast<PartGui::ViewProviderPartExt*>(gui->getViewProvider(source));
            auto* resultView = freecad_cast<PartGui::ViewProviderPartExt*>(gui->getViewProvider(result));
            if (!sourceView || !resultView) {
                throw Base::ValueError("The planar offset could not be displayed.");
            }
            copyAppearance(*source, *result, face.c_str(), *sourceView, *resultView);
            sourceView->hide();
            resultView->show();
            transaction.close();
        }
        catch (...) {
            transaction.close(App::TransactionCloseMode::Abort);
            Gui::getMainWindow()->showMessage(tr("The face edit could not be applied."), 5000);
        }
    }
};

}  // namespace

void registerSvenPlanarOffset(Gui::CommandManager& commands)
{
    commands.addCommand(new CmdSvenPlanarOffset);
}
