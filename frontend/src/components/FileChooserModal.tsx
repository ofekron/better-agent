import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";

interface Props {
  open: boolean;
  cwd: string;
  /** Multi-machine: which node's filesystem the tree browses. */
  nodeId?: string;
  onFileClick: (path: string) => void;
  onEngineerFile: (path: string) => void;
  onClose: () => void;
}

import { FileTree } from "./FileTree";

/** Modal file browser — opens a file in the right panel on click. */
export function FileChooserModal({
  open, cwd, nodeId = "primary", onFileClick, onEngineerFile, onClose,
}: Props) {
  const { t } = useTranslation();
  useBackButtonDismiss(open, onClose);

  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-content file-chooser-content"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2>{t("fileChooser.title")}</h2>
          <button className="modal-close" onClick={onClose}>
            &times;
          </button>
        </div>

        <div className="modal-body file-chooser-body">
          <FileTree
            cwd={cwd}
            nodeId={nodeId}
            onFileClick={(path) => {
              onFileClick(path);
              onClose();
            }}
            onEngineerFile={(path) => {
              onEngineerFile(path);
              onClose();
            }}
          />
        </div>
      </div>
    </div>
  );
}
