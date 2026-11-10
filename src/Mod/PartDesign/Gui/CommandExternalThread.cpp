// SPDX-License-Identifier: LGPL-2.1-or-later

#include <algorithm>
#include <string>
#include <type_traits>
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
#include <App/DocumentObserver.h>
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
#include <Mod/PartDesign/App/FeatureInternalThread.h>
#include <Mod/PartDesign/App/SvenMetricThreadLimits.h>

namespace
{
using PartDesign::ExternalThread;
using PartDesign::InternalThread;

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

ThreadSelection resolveTarget(const App::SubObjectT& ref, const App::Document* document,
                              const char* selectionError)
{
    int index = 0;
    auto* source = freecad_cast<Part::Feature*>(ref.getSubObject());
    const auto path = ref.getSubObjectList();
    if (!source || !source->isAttachedToDocument() || source->getDocument() != document
        || ref.getOldElementName(&index) != "Face" || index < 1
        || path.empty() || path.back() != source) {
        throw Base::ValueError(selectionError);
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

class CmdPartDesignThread: public Gui::Command
{
public:
    explicit CmdPartDesignThread(bool internal)
        : Gui::Command(internal ? "PartDesign_InternalThread" : "PartDesign_ExternalThread")
        , internal(internal)
    {
        sAppModule = "PartDesign";
        sGroup = QT_TR_NOOP("Part Design");
        sMenuText = internal ? QT_TRANSLATE_NOOP("CmdPartDesignExternalThread", "Internal Thread")
                             : QT_TRANSLATE_NOOP("CmdPartDesignExternalThread", "External Thread");
        sToolTipText = internal
            ? QT_TRANSLATE_NOOP("CmdPartDesignExternalThread", "Create a metric thread on an existing cylindrical hole wall")
            : QT_TRANSLATE_NOOP("CmdPartDesignExternalThread", "Create a nominal metric external thread on a shaft");
        sStatusTip = sToolTipText;
        sPixmap = internal ? "PartDesign_SubtractiveHelix" : "PartDesign_AdditiveHelix";
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
            QMessageBox::warning(Gui::getMainWindow(), threadText(sMenuText),
                                 QCoreApplication::translate("Exception", selectionMessage()));
            return;
        }
        try {
            if (internal) {
                addThread<InternalThread>(selection.front(), document);
            }
            else {
                addThread<ExternalThread>(selection.front(), document);
            }
        }
        catch (const Base::Exception& error) {
            auto message = QCoreApplication::translate("Exception", error.what());
            if (internal && message == QString::fromUtf8(error.what())) {
                message = QCoreApplication::translate("SvenPlanarOffset", error.what());
            }
            QMessageBox::warning(Gui::getMainWindow(), threadText(sMenuText), message);
        }
        catch (const Standard_Failure&) {
            QMessageBox::warning(
                Gui::getMainWindow(), threadText(sMenuText),
                QCoreApplication::translate("Exception", internal
                    ? QT_TRANSLATE_NOOP("Exception", "The geometry kernel could not build the internal thread.")
                    : QT_TRANSLATE_NOOP("Exception", "The geometry kernel could not build the external thread."))
            );
        }
    }

private:
    const char* selectionMessage() const
    {
        return internal ? QT_TRANSLATE_NOOP("Exception", "Select one internal cylindrical hole wall.")
                        : QT_TRANSLATE_NOOP("Exception", "Select one exterior cylindrical face of a solid.");
    }

    template<class Thread>
    void addThread(const App::SubObjectT& ref, App::Document* document)
    {
        constexpr bool isInternal = std::is_same_v<Thread, InternalThread>;
        const auto selection = resolveTarget(ref, document, selectionMessage());
        const auto target = Thread::describe(*selection.source, selection.face.c_str());
        const App::DocumentWeakPtrT originalDocument(document);
        const auto prefix = isInternal ? QStringLiteral("InternalThread") : QStringLiteral("ExternalThread");
        const auto maxLength = [&] {
            if constexpr (isInternal) {
                return target.depth;
            }
            else {
                return target.length;
            }
        }();

        QDialog dialog(Gui::getMainWindow());
        dialog.setObjectName(prefix + QStringLiteral("Dialog"));
        dialog.setWindowTitle(threadText(sMenuText));
        auto* form = new QFormLayout(&dialog);
        auto* size = new QComboBox(&dialog);
        size->setObjectName(prefix + QStringLiteral("Size"));
        for (const char** item = ExternalThread::Sizes; *item; ++item) {
            size->addItem(QString::fromLatin1(*item));
        }
        size->setCurrentIndex(target.size);
        form->addRow(threadText("Size"), size);

        auto* manufacturingClass = new QComboBox(&dialog);
        manufacturingClass->setObjectName(prefix + QStringLiteral("ManufacturingClass"));
        if constexpr (isInternal) {
            manufacturingClass->addItem(QStringLiteral("6H"));
        }
        else {
            manufacturingClass->addItem(threadText("Unspecified"));
            manufacturingClass->addItem(QStringLiteral("6g"));
        }
        form->addRow(threadText("Manufacturing class"), manufacturingClass);
        auto* manufacturingLimits = new QLabel(&dialog);
        manufacturingLimits->setObjectName(prefix + QStringLiteral("ManufacturingLimits"));
        manufacturingLimits->setWordWrap(true);
        form->addRow(manufacturingLimits);
        const auto updateLimits = [=] {
            const auto* limits = PartDesign::MetricCoarseThreadLimits::find(
                size->currentText().toStdString()
            );
            const bool show = limits && (isInternal || manufacturingClass->currentIndex() == 1);
            manufacturingLimits->setVisible(show);
            if (show) {
                manufacturingLimits->setText(
                    QString::fromStdString(isInternal ? limits->internalReference() : limits->externalReference())
                    + QStringLiteral("\n")
                    + threadText("The model uses nominal dimensions.")
                );
            }
        };
        QObject::connect(size, qOverload<int>(&QComboBox::currentIndexChanged), &dialog, updateLimits);
        QObject::connect(manufacturingClass, qOverload<int>(&QComboBox::currentIndexChanged), &dialog, updateLimits);
        updateLimits();

        auto* length = new Gui::QuantitySpinBox(&dialog);
        length->setObjectName(prefix + QStringLiteral("Length"));
        length->setUnit(Base::Unit::Length);
        length->setDecimals(6);
        length->setMinimum(0.0001);
        length->setMaximum(maxLength);
        length->setValue(std::min(maxLength, 4.0 * ExternalThread::pitchForSize(target.size)));
        form->addRow(threadText("Thread length"), length);

        auto* hand = new QComboBox(&dialog);
        hand->setObjectName(prefix + QStringLiteral("Hand"));
        hand->addItem(threadText("Right-hand"));
        hand->addItem(threadText("Left-hand"));
        form->addRow(threadText("Hand"), hand);

        QCheckBox* reverseStart = nullptr;
        if constexpr (isInternal) {
            if (target.startOpen && target.endOpen) {
                reverseStart = new QCheckBox(threadText("Start at the opposite opening"), &dialog);
                reverseStart->setObjectName(QStringLiteral("InternalThreadReverseStart"));
                form->addRow(reverseStart);
            }
        }

        auto* modeled = new QCheckBox(threadText("Model thread grooves"), &dialog);
        modeled->setObjectName(prefix + QStringLiteral("Modeled"));
        modeled->setChecked(true);
        form->addRow(modeled);
        auto* note = new QLabel(threadText(isInternal
            ? QT_TRANSLATE_NOOP("CmdPartDesignExternalThread", "Clear the checkbox to keep the original bore geometry and save the thread parameters.")
            : QT_TRANSLATE_NOOP("CmdPartDesignExternalThread", "Clear the checkbox to save thread parameters with the original shaft geometry.")), &dialog);
        note->setWordWrap(true);
        form->addRow(note);
        auto* validation = new QLabel(&dialog);
        validation->setWordWrap(true);
        form->addRow(validation);

        auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
        form->addRow(buttons);
        QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
        QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, [&] {
            if (size->currentIndex() != target.size) {
                validation->setText(threadText(isInternal
                    ? QT_TRANSLATE_NOOP("CmdPartDesignExternalThread", "Choose the nominal size that matches the bore diameter.")
                    : QT_TRANSLATE_NOOP("CmdPartDesignExternalThread", "Choose the nominal size that matches the shaft diameter.")));
            }
            else if (!length->hasValidInput() || length->rawValue() <= 0.0) {
                validation->setText(threadText("Enter a positive thread length."));
            }
            else {
                dialog.accept();
            }
        });
        if (dialog.exec() != QDialog::Accepted || *originalDocument != document
            || document != getActiveDocument()) {
            return;
        }

        const auto current = resolveTarget(ref, document, selectionMessage());
        const auto refreshed = Thread::describe(*current.source, current.face.c_str());
        const auto threadLength = length->rawValue();
        const auto leftHand = hand->currentIndex() == 1;
        const auto modelThread = modeled->isChecked();
        const auto threadSize = size->currentIndex();
        const auto opposite = reverseStart && reverseStart->isChecked();
        const auto resultShape = [&] {
            if constexpr (isInternal) {
                return Thread::makeShape(refreshed, threadSize, threadLength, leftHand, modelThread, opposite);
            }
            else {
                return Thread::makeShape(refreshed, threadSize, threadLength, leftHand, modelThread);
            }
        }();
        if (document->hasPendingTransaction() || document->getBookedTransactionID() != App::NullTransaction) {
            throw Base::RuntimeError(QT_TRANSLATE_NOOP("Exception", "Finish the current document operation before threading."));
        }
        const auto transactionId = document->openTransaction(sMenuText);
        if (transactionId == App::NullTransaction) {
            throw Base::RuntimeError(QT_TRANSLATE_NOOP("Exception", "The document could not start a thread transaction."));
        }
        App::AutoTransaction transaction(transactionId);
        try {
            auto* result = document->addObject<Thread>(isInternal ? "InternalThread" : "ExternalThread");
            if (!result) {
                throw Base::RuntimeError(QT_TRANSLATE_NOOP("Exception", "The thread could not be added."));
            }
            if (current.parent) {
                current.parent->addObject(result);
            }
            result->Label.setValue(threadText(isInternal
                ? QT_TRANSLATE_NOOP("CmdPartDesignExternalThread", "Internal Thread %1")
                : QT_TRANSLATE_NOOP("CmdPartDesignExternalThread", "External Thread %1"))
                .arg(size->currentText()).toUtf8().constData());
            result->Support.setValue(current.source, {current.face});
            result->ThreadSize.setValue(static_cast<long>(threadSize));
            result->ThreadLength.setValue(threadLength);
            result->LeftHanded.setValue(leftHand);
            result->Modeled.setValue(modelThread);
            if constexpr (isInternal) {
                result->ReverseStart.setValue(opposite);
                result->setThreadResult(*current.source, refreshed, resultShape);
            }
            else {
                result->ManufacturingClass.setValue(static_cast<long>(manufacturingClass->currentIndex()));
                result->setThreadResult(*current.source, resultShape);
            }
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

    bool internal;
};

}  // namespace

void CreateExternalThreadCommand()
{
    auto& commands = Gui::Application::Instance->commandManager();
    commands.addCommand(new CmdPartDesignThread(false));
    commands.addCommand(new CmdPartDesignThread(true));
}
