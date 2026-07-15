export interface ComposerSubmissionTransaction<T> {
  payload: T;
  allowed: boolean;
  submit: (payload: T) => boolean | Promise<boolean>;
  begin: (payload: T) => void;
  commit: (payload: T) => void;
  rollback: (payload: T) => void;
}

export async function executeComposerSubmission<T>(
  transaction: ComposerSubmissionTransaction<T>,
): Promise<boolean> {
  if (!transaction.allowed) return false;
  transaction.begin(transaction.payload);
  let submitted = false;
  try {
    submitted = await transaction.submit(transaction.payload);
    if (!submitted) {
      transaction.rollback(transaction.payload);
      return false;
    }
    transaction.commit(transaction.payload);
    return true;
  } catch (error) {
    transaction.rollback(transaction.payload);
    throw error;
  }
}
