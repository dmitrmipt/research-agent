"use strict";
(() => {
  // Keep in sync with math_markup.py. Limit each fragment, including older saved profiles.
  const fragments = /(?<!\\)(?:\$\$([^$]{1,1500})\$\$|\\\[([\s\S]{1,1500}?)\\\]|\\\(([^\r\n]{1,1500}?)\\\)|(?<!\$)\$(?!\s)((?:\\[^\r\n]|[^$\\\r\n]){1,1500}?)(?<!\s)\$(?![\d$]))/g;

  function renderMath(node, mode = "display") {
    if (!window.katex || !node.textContent) return;
    const source = node.textContent;
    const result = document.createDocumentFragment();
    let end = 0;
    for (const match of source.matchAll(fragments)) {
      const tex = (match[1] ?? match[2] ?? match[3] ?? match[4]).trim();
      if (!tex) continue;
      result.append(source.slice(end, match.index));
      const displayMode = mode === "display" && (match[1] !== undefined || match[2] !== undefined);
      end = match.index + match[0].length;
      const punctuation = displayMode ? /^[ \t]*([.,;:!?])(?=\s|$)/.exec(source.slice(end)) : null;
      const formula = document.createElement("span");
      formula.className = displayMode ? "profile-formula formula-display" : "profile-formula formula-inline";
      try {
        window.katex.render(tex + (punctuation?.[1] || ""), formula,
          {output: "mathml", displayMode, trust: false, maxExpand: 200, maxSize: 10});
        result.append(formula);
        if (punctuation) end += punctuation[0].length;
      } catch {
        result.append(match[0]);
      }
    }
    if (end) {
      result.append(source.slice(end));
      node.replaceChildren(result);
    }
  }

  window.renderProfileMath = renderMath;
  for (const node of document.querySelectorAll(".profile-math-source")) renderMath(node);
})();
