import React, { useState } from "react";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogCancel,
  AlertDialogAction,
} from "@/components/ui/alert-dialog";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * TwoStepDeleteDialog
 *
 * Implements strict two-step delete confirmation:
 * Step 1: Reconsider dialog
 *   - "Are you sure you want to delete this?"
 *   - "You are about to permanently remove this data from LedgerLens.
 *      After deletion, you will not be able to see or recover it in LedgerLens.
 *      Your original files on your computer will NOT be deleted."
 *   - Buttons: Cancel (default/focused), Continue to Delete
 *
 * Step 2: Final confirmation
 *   - "Final confirmation"
 *   - "This data will no longer be available in LedgerLens.
 *      Do you want to permanently delete it?"
 *   - Buttons: Keep It (safe), Delete Permanently (destructive)
 */
export function TwoStepDeleteDialog({
  open,
  onOpenChange,
  onConfirm,
  itemName = "",
}) {
  const [step, setStep] = useState(1);

  const handleOpenChange = (nextOpen) => {
    if (!nextOpen) {
      setStep(1);
    }
    if (onOpenChange) {
      onOpenChange(nextOpen);
    }
  };

  const handleCancel = () => {
    setStep(1);
    if (onOpenChange) onOpenChange(false);
  };

  const handleContinue = () => {
    setStep(2);
  };

  const handleFinalDelete = async () => {
    setStep(1);
    if (onOpenChange) onOpenChange(false);
    if (onConfirm) {
      await onConfirm();
    }
  };

  return (
    <AlertDialog open={open} onOpenChange={handleOpenChange}>
      <AlertDialogContent
        data-testid="two-step-delete-dialog"
        className="sm:max-w-[460px]"
      >
        {step === 1 ? (
          <>
            <AlertDialogHeader>
              <AlertDialogTitle data-testid="delete-step1-title" className="text-foreground text-lg font-semibold">
                Are you sure you want to delete this?
              </AlertDialogTitle>
              <AlertDialogDescription
                data-testid="delete-step1-desc"
                className="text-sm text-muted-foreground space-y-2 mt-2"
                asChild
              >
                <div>
                  <p>
                    {itemName
                      ? `You are about to permanently remove "${itemName}" and associated data from LedgerLens.`
                      : "You are about to permanently remove this data from LedgerLens."}
                  </p>
                  <p className="font-medium text-foreground">
                    After deletion, you will not be able to see or recover it in LedgerLens.
                  </p>
                  <p className="text-xs text-muted-foreground/90 border-t border-border pt-2 mt-2">
                    Your original files on your computer will <span className="font-semibold text-foreground">NOT</span> be deleted.
                  </p>
                </div>
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter className="mt-4 sm:space-x-2">
              <AlertDialogCancel
                data-testid="delete-step1-cancel"
                autoFocus
                onClick={handleCancel}
                className="hover:bg-secondary cursor-pointer"
              >
                Cancel
              </AlertDialogCancel>
              <button
                type="button"
                data-testid="delete-step1-continue"
                onClick={handleContinue}
                className={cn(
                  buttonVariants({ variant: "destructive" }),
                  "bg-rose-600 hover:bg-rose-700 text-white cursor-pointer"
                )}
              >
                Continue to Delete
              </button>
            </AlertDialogFooter>
          </>
        ) : (
          <>
            <AlertDialogHeader>
              <AlertDialogTitle data-testid="delete-step2-title" className="text-foreground text-lg font-semibold text-rose-600 dark:text-rose-400">
                Final confirmation
              </AlertDialogTitle>
              <AlertDialogDescription
                data-testid="delete-step2-desc"
                className="text-sm text-muted-foreground space-y-2 mt-2"
                asChild
              >
                <div>
                  <p>
                    This data will no longer be available in LedgerLens.
                  </p>
                  <p className="font-medium text-foreground">
                    Do you want to permanently delete it?
                  </p>
                </div>
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter className="mt-4 sm:space-x-2">
              <AlertDialogCancel
                data-testid="delete-step2-keep"
                autoFocus
                onClick={handleCancel}
                className="hover:bg-secondary cursor-pointer font-medium"
              >
                Keep It
              </AlertDialogCancel>
              <AlertDialogAction
                data-testid="delete-step2-confirm"
                onClick={handleFinalDelete}
                className={cn(
                  buttonVariants({ variant: "destructive" }),
                  "bg-rose-600 hover:bg-rose-700 text-white cursor-pointer font-semibold"
                )}
              >
                Delete Permanently
              </AlertDialogAction>
            </AlertDialogFooter>
          </>
        )}
      </AlertDialogContent>
    </AlertDialog>
  );
}
