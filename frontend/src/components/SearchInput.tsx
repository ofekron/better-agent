import { forwardRef, type InputHTMLAttributes } from "react";

type SearchInputProps = InputHTMLAttributes<HTMLInputElement>;

// Android's WebView predictive-text IME corrupts React controlled inputs:
// composition events race React's value writeback and reset the caret to
// position 0, so each new character is inserted at the front and the typed
// text reads back reversed. Turning the field into a plain raw-text input
// (no autocorrect / autocomplete / autocapitalize / spellcheck) removes the
// composition path. Harmless on desktop; search filters don't want autocorrect
// guessing file/session names anyway. Protective attrs are applied last so a
// caller can never accidentally re-enable the IME composition path.
export const SearchInput = forwardRef<HTMLInputElement, SearchInputProps>(
  function SearchInput(props, ref) {
    return (
      <input
        {...props}
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        spellCheck={false}
        ref={ref}
      />
    );
  },
);
