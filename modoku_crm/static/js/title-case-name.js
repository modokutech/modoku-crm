/* Auto-capitalizes participant "name" inputs to Title Case as the user
 * types, mirroring t3._title_case_name() on the back end (belt and
 * suspenders - the server does the same transform in case JS is off,
 * or an input reaches the server some other way).
 *
 * Rule: split on whitespace, capitalize the first alphabetic character
 * of each word, lowercase the rest. E.g. "ALI BIN ABU" / "aLi bIn AbU"
 * -> "Ali Bin Abu". Deliberately simple - no "Mc"/hyphen/apostrophe
 * special-casing.
 *
 * Transforms live on `input` (word-by-word, cursor-safe) and again on
 * `blur` (covers paste and anything the live pass missed). Applies to
 * any <input name="name"> on the page - safe to include on any form
 * that may or may not have one.
 */
(function () {
  function titleCaseWord(word) {
    if (!word) return word;
    return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
  }

  function titleCase(value) {
    return value.split(" ").map(titleCaseWord).join(" ");
  }

  function attach(input) {
    if (input.dataset.titleCaseBound) return;
    input.dataset.titleCaseBound = "1";

    // Live, cursor-safe transform: only re-case the *word currently being
    // typed* up to the caret, so we never fight the user mid-keystroke on
    // words elsewhere in the field.
    input.addEventListener("input", function () {
      var pos = input.selectionStart;
      var value = input.value;
      if (pos === null || pos === undefined) {
        input.value = titleCase(value);
        return;
      }
      var before = value.slice(0, pos);
      var after = value.slice(pos);
      // Re-case only the word the caret is currently inside of (the
      // text since the last space before the caret).
      var lastSpace = before.lastIndexOf(" ");
      var head = before.slice(0, lastSpace + 1);
      var word = before.slice(lastSpace + 1);
      var newBefore = head + titleCaseWord(word);
      input.value = newBefore + after;
      input.setSelectionRange(newBefore.length, newBefore.length);
    });

    // Final safety net: full-string transform on blur (covers paste,
    // autofill, and anything the live per-word pass didn't catch).
    input.addEventListener("blur", function () {
      input.value = titleCase(input.value);
    });
  }

  function scan() {
    document.querySelectorAll('input[name="name"]').forEach(attach);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scan);
  } else {
    scan();
  }

  // Some of these forms add rows dynamically (inline add/edit rows) -
  // watch for new name inputs being inserted and wire them up too.
  var observer = new MutationObserver(function () { scan(); });
  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
