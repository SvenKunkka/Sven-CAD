// SPDX-License-Identifier: LGPL-2.1-or-later

#include <algorithm>
#include <string>
#include <utility>

#include <QCheckBox>
#include <QComboBox>
#include <QCoreApplication>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFormLayout>
#include <QLabel>
#include <QMessageBox>
#include <QPushButton>

#include <Standard_Failure.hxx>

#include <App/AutoTransaction.h>
#include <App/Document.h>
#include <App/GeoFeatureGroupExtension.h>
#include <App/Part.h>
#include <Base/Exception.h>
#include <Gui/Application.h>
#include <Gui/Command.h>
#include <Gui/Control.h>
#include <Gui/Document.h>
#include <Gui/MainWindow.h>
#include <Gui/QuantitySpinBox.h>
#include <Gui/Selection/Selection.h>
#include <Mod/Part/Gui/ViewProviderExt.h>
#include <Mod/PartDesign/App/FeatureExternalThread.h>

namespace
{

QString threadText(const char* source)
{
    return QCoreApplication::translate("CmdPartDesignExternalThread", source);
}

struct ThreadSelection
{
    Part::Feature* source;
    App::Part* parent;
    std::string face;
};

ThreadSelection resolveShaft(const App::SubObjectT& ref, const App::Document* document)
{
    int index = 0;
    auto* source = freecad_cast<Part::Feature*>(ref.getSubObject());
    const auto path = ref.getSubObjectList();
    if (!source || !source->isAttachedToDocument() || source->getDocument() != document
        || ref.getOldElementName(&index) != "Face" || index < 1
        || path.empty() || path.back() != source) {
        throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Select one exterior cylindrical face of a solid."));
    }

    for (std::size_t i = 0; i + 1 < path.size(); ++i) {
        if (path[i]->getTypeId() != App::Part::getClassTypeId()
            || App::GeoFeatureGroupExtension::getGroupOfObject(path[i + 1]) != path[i]) {
            throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Select the source shaft instead of an instance."));
        }
    }
    auto* group = App::GeoFeatureGroupExtension::getGroupOfObject(source);
    for (auto* ancestor = group; ancestor; ancestor = App::GeoFeatureGroupExtension::getGroupOfObject(ancestor)) {
        if (ancestor->getTypeId() != App::Part::getClassTypeId()) {
            throw Base::ValueError(QT_TRANSLATE_NOOP("Exception", "Select a shaft outside a Part Design body."));
        }
    }
    auto face = ref.getNewElementName();
    if (face.empty()) {
        face = ref.getElementName();
    }
    return {source, static_cast<App::Part*>(group), std::move(face)};
}

class CmdPartDesignExternalThread: public Gui::Command
{
public:
    CmdPartDesignExternalThread()
        : Gui::Command("PartDesign_ExternalThread")
    {
        sAppModule = "PartDesign";
        sGroup = QT_TR_NOOP("Part Design");
        sMenuText = QT_TRANSLATE_NOOP("CmdPartDesignExternalThread", "External Thread");
        sToolTipText = QT_TRANSLATE_NOOP("CmdPartDesignExternalThread", "Create a nominal metric external thread on a shaft");
        sStatusTip = sToolTipText;
        sPixmap = "PartDesign_AdditiveHelix";
        eType = ForEdit;
    }

    const char* className() const override
    {
        return "CmdPartDesignExternalThread";
    }

    bool isActive() override
    {
        auto* document = getActiveGuiDocument();
        return document && !document->getInEdit() && !Gui::Control().activeDialog();
    }

protected:
    void activated(int) override
    {
        auto* document = getActiveDocument();
        const auto selection = Gui::Selection().getSelectionT(nullptr, Gui::ResolveMode::NoResolve, true);
        if (!document || selection.size() != 1) {
            QMessageBox::warning(Gui::getMainWindow(), threadText("External Thread"),
                                 threadText("Select one exterior cylindrical face of a solid."));
            return;
        }
        try {
            addThread(selection.front(), document);
        }
        catch (const Base::Exception& error) {
            QMessageBox::warning(Gui::getMainWindow(), threadText("External Thread"),
                                 QCoreApplication::translate("Exception", error.what()));
        }
        catch (const Standard_Failure&) {
            QMessageBox::warning(
                Gui::getMainWindow(), threadText("External Thread"),
                QCoreApplication::translate("Exception", "The geometry kernel could not build the external thread.")
            );
        }
    }

private:
    void addThread(const App::SubObjectT& ref, App::Document* document)
    {
        using PartDesign::ExternalThread;
        const auto selection = resolveShaft(ref, document);
        const auto shaft = ExternalThread::describe(*selection.source, selection.face.c_str());

        QDialog dialog(Gui::getMainWindow());
        dialog.setObjectName(QStringLiteral("ExternalThreadDialog"));
        dialog.setWindowTitle(threadText("External Thread"));
        auto* form = new QFormLayout(&dialog);
        auto* size = new QComboBox(&dialog);
        size->setObjectName(QStringLiteral("ExternalThreadSize"));
        for (const char** item = ExternalThread::Sizes; *item; ++item) {
            size->addItem(QString::fromLatin1(*item));
        }
        size->setCurrentIndex(shaft.size);
        form->addRow(threadText("Size"), size);

        auto* length = new Gui::QuantitySpinBox(&dialog);
        length->setObjectName(QStringLiteral("ExternalThreadLength"));
        length->setUnit(Base::Unit::Length);
        length->setDecimals(6);
        length->setMinimum(0.0001);
        length->setMaximum(shaft.length);
        length->setValue(std::min(shaft.length, 4.0 * ExternalThread::pitchForSize(shaft.size)));
        form->addRow(threadText("Thread length"), length);

        auto* hand = new QComboBox(&dialog);
        hand->setObjectName(QStringLiteral("ExternalThreadHand"));
        hand->addItem(threadText("Right-hand"));
        hand->addItem(threadText("Left-hand"));
        form->addRow(threadText("Hand"), hand);

        auto* modeled = new QCheckBox(threadText("Model thread grooves"), &dialog);
        modeled->setObjectName(QStringLiteral("ExternalThreadModeled"));
        modeled->setChecked(true);
        form->addRow(modeled);
        auto* note = new QLabel(threadText("Clear the checkbox to save thread parameters with the original shaft geometry."), &dialog);
        note->setWordWrap(true);
        form->addRow(note);
        auto* validation = new QLabel(&dialog);
        validation->setWordWrap(true);
        form->addRow(validation);

        auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
        form->addRow(buttons);
        QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
        QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, [&] {
            if (size->currentIndex() != shaft.size) {
                validation->setText(threadText("Choose the nominal size that matches the shaft diameter."));
            }
            else if (!length->hasValidInput() || length->rawValue() <= 0.0) {
                validation->setText(threadText("Enter a positive thread length."));
            }
            else {
                dialog.accept();
            }
        });
        if (dialog.exec() != QDialog::Accepted || document != getActiveDocument()) {
            return;
        }

        const auto current = resolveShaft(ref, document);
        const auto threadLength = length->rawValue();
        const auto leftHand = hand->currentIndex() == 1;
        const auto modelThread = modeled->isChecked();
        const auto threadSize = size->currentIndex();
        const auto resultShape = ExternalThread::makeShape(
            ExternalThread::describe(*current.source, current.face.c_str()),
            threadSize, threadLength, leftHand, modelThread
        );
        if (document->getBookedTransactionID() != App::NullTransaction) {
            throw Base::RuntimeError(QT_TRANSLATE_NOOP("Exception", "Finish the current document operation before threading."));
        }
        const auto transactionId = document->openTransaction("External Thread");
        if (transactionId == App::NullTransaction) {
            throw Base::RuntimeError(QT_TRANSLATE_NOOP("Exception", "The document could not start a thread transaction."));
        }
        App::AutoTransaction transaction(transactionId);
        try {
            auto* result = document->addObject<ExternalThread>("ExternalThread");
            if (!result) {
                throw Base::RuntimeError(QT_TRANSLATE_NOOP("Exception", "The external thread could not be added."));
            }
            if (current.parent) {
                current.parent->addObject(result);
            }
            result->Label.setValue(threadText("External Thread %1").arg(size->currentText()).toUtf8().constData());
            result->Support.setValue(current.source, {current.face});
            result->ThreadSize.setValue(static_cast<long>(threadSize));
            result->ThreadLength.setValue(threadLength);
            result->LeftHanded.setValue(leftHand);
            result->Modeled.setValue(modelThread);
            result->setThreadResult(*current.source, resultShape);
            result->purgeTouched();

            auto* app = Gui::Application::Instance;
            auto* sourceView = app->getViewProvider<PartGui::ViewProviderPartExt>(current.source);
            auto* resultView = app->getViewProvider<PartGui::ViewProviderPartExt>(result);
            if (sourceView && resultView) {
                const auto& appearance = sourceView->ShapeAppearance.getValues();
                if (appearance.size() == 1 || (!modelThread && !appearance.empty())) {
                    resultView->ShapeAppearance.setValues(appearance);
                }
            }
            if (auto* view = app->getViewProvider(current.source)) {
                view->hide();
            }
            transaction.close();
        }
        catch (...) {
            transaction.close(App::TransactionCloseMode::Abort);
            throw;
        }
    }
};

}  // namespace

void CreateExternalThreadCommand()
{
    Gui::Application::Instance->commandManager().addCommand(new CmdPartDesignExternalThread());
}
