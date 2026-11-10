// SPDX-License-Identifier: LGPL-2.1-or-later

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

#include <Standard_Failure.hxx>

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

void copyAppearance(
    Part::Feature& source,
    Part::Feature& result,
    const char* supportFace,
    PartGui::ViewProviderPartExt& sourceView,
    PartGui::ViewProviderPartExt& resultView
)
{
    const auto& materials = sourceView.ShapeAppearance.getValues();
    if (materials.size() <= 1) {
        resultView.ShapeAppearance.setValues(materials);
        return;
    }

    // Use the selected face's material for a new cap if the Boolean operation did not map it.
    int selectedIndex = 0;
    App::SubObjectT(&source, supportFace).getOldElementName(&selectedIndex);
    const auto fallback = selectedIndex > 0 && static_cast<size_t>(selectedIndex) <= materials.size()
        ? materials[selectedIndex - 1]
        : materials.front();
    const auto faceCount = result.Shape.getShape().countSubShapes(TopAbs_FACE);
    std::vector<App::Material> appearance(faceCount, fallback);
    for (size_t i = 0; i < materials.size(); ++i) {
        const auto name = "Face" + std::to_string(i + 1);
        for (const auto& face : Part::Feature::getElementFromSource(&result, "", &source, name.c_str())) {
            const auto index = face.index.getIndex();
            if (index > 0 && static_cast<size_t>(index) <= appearance.size()) {
                appearance[index - 1] = materials[i];
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
        sToolTipText = QT_TR_NOOP("Offsets a planar face of one STEP solid");
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
        auto selection = Gui::Selection().getSelectionT(nullptr, Gui::ResolveMode::NoResolve, true);
        auto* source = selection.size() == 1 ? selectedSource(selection.front()) : nullptr;
        if (!source) {
            Gui::getMainWindow()->showMessage(tr("Select one planar face on a STEP solid."), 5000);
            return;
        }
        auto* view = freecad_cast<PartGui::ViewProviderPartExt*>(
            getActiveGuiDocument()->getViewProvider(source)
        );
        if (!view || !view->isShow()) {
            Gui::getMainWindow()->showMessage(tr("Show the source solid before offsetting its face."), 5000);
            return;
        }
        const auto& reference = selection.front();
        auto face = reference.getNewElementName();
        if (face.empty()) {
            face = reference.getElementName();
        }

        QDialog dialog(Gui::getMainWindow());
        dialog.setObjectName(QStringLiteral("SvenPlanarOffsetDialog"));
        dialog.setWindowTitle(tr("Offset Planar Face"));
        auto* layout = new QFormLayout(&dialog);
        auto* distance = new QDoubleSpinBox(&dialog);
        distance->setObjectName(QStringLiteral("SvenPlanarOffsetDistance"));
        distance->setDecimals(4);
        distance->setRange(-1000000.0, 1000000.0);
        distance->setSingleStep(0.1);
        distance->setSuffix(QStringLiteral(" mm"));
        distance->setValue(1);
        layout->addRow(tr("Distance"), distance);
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
                auto result = SvenPlanarOffset::calculate(*support, face.c_str(), distance->value());
                // Keep viewport meshing off the live source topology.
                auto display = result.makeElementCopy(nullptr, false, false);
                PartGui::ViewProviderPartExt::setupCoinGeometry(
                    display.getShape(),
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
                status->setText(tr("Positive moves outward. Negative moves inward."));
                okay->setEnabled(true);
            }
            catch (const Base::Exception& error) {
                status->setText(QCoreApplication::translate("SvenPlanarOffset", error.what()));
            }
            catch (const Standard_Failure&) {
                status->setText(tr("The geometry engine could not preview this offset."));
            }
            catch (const std::exception&) {
                status->setText(tr("The geometry engine could not preview this offset."));
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
        commit(reference, face, distance->value());
    }

private:
    void commit(const App::SubObjectT& reference, const std::string& face, double distance)
    {
        auto* source = selectedSource(reference);
        auto* document = getActiveDocument();
        auto* gui = getActiveGuiDocument();
        if (!source || !gui || source->getDocument() != document || document->hasPendingTransaction()
            || document->getBookedTransactionID() != App::NullTransaction) {
            Gui::getMainWindow()->showMessage(tr("The source or document changed before confirmation."), 5000);
            return;
        }
        const int transactionId = document->openTransaction(tr("Offset Planar Face").toStdString());
        if (transactionId == App::NullTransaction) {
            Gui::getMainWindow()->showMessage(tr("The planar offset could not be applied."), 5000);
            return;
        }
        App::AutoTransaction transaction(transactionId);
        try {
            auto* result = static_cast<SvenPlanarOffset*>(
                document->addObject("Part::SvenPlanarOffset", "PlanarOffset")
            );
            if (auto* parent = App::GeoFeatureGroupExtension::getGroupOfObject(source)) {
                static_cast<App::Part*>(parent)->addObject(result);
            }
            result->Support.setValue(source, std::vector<std::string>{face});
            result->Offset.setValue(distance);
            if (!result->recomputeFeature()) {
                throw Base::ValueError("The planar offset could not be applied.");
            }
            auto* sourceView = freecad_cast<PartGui::ViewProviderPartExt*>(gui->getViewProvider(source));
            auto* resultView = freecad_cast<PartGui::ViewProviderPartExt*>(gui->getViewProvider(result));
            if (!sourceView || !resultView) {
                throw Base::ValueError("The planar offset could not be displayed.");
            }
            copyAppearance(*source, *result, face.c_str(), *sourceView, *resultView);
            sourceView->hide();
            transaction.close();
        }
        catch (...) {
            transaction.close(App::TransactionCloseMode::Abort);
            Gui::getMainWindow()->showMessage(tr("The planar offset could not be applied."), 5000);
        }
    }
};

}  // namespace

void registerSvenPlanarOffset(Gui::CommandManager& commands)
{
    commands.addCommand(new CmdSvenPlanarOffset);
}
