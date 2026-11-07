/*!
 * skeleton.js 1.0 — builds skeleton screens from the live DOM.
 *
 * Nothing is hand-drawn. You render the real layout (with placeholder data) and
 * Skeleton.mask() measures it and turns every piece of text, image, icon and form
 * field into a shimmering bone of the same size, in the same place. Cards, rows,
 * separators and spacing stay real, so the skeleton always matches the page, even
 * when the page's markup or CSS changes.
 *
 *   Skeleton.mask(el)         mask one element's subtree
 *   Skeleton.maskAll(scope)   mask every [data-skeleton] inside scope (default: document)
 *   Skeleton.unmask(el)       restore (not needed if you just re-render the region)
 *
 * Markup hints:  data-sk-skip  keep this subtree as-is (e.g. a chevron or a title)
 */
(function (global) {
  "use strict";

  var STYLE_ID = "skeleton-js-style";
  var CSS =
    ":root{--sk-base:rgba(120,120,128,.13);--sk-shine:rgba(120,120,128,.24)}" +
    ".sk-t,.sk-b{color:transparent!important;-webkit-text-fill-color:transparent!important;" +
    "background:linear-gradient(90deg,var(--sk-base) 25%,var(--sk-shine) 50%,var(--sk-base) 75%) 0 0/200% 100%!important;" +
    "animation:sk-shimmer 1.4s ease-in-out infinite;pointer-events:none;user-select:none;-webkit-user-select:none;" +
    "border-color:transparent!important;box-shadow:none!important;text-shadow:none!important;caret-color:transparent;" +
    "border-radius:var(--sk-r,6px);overflow:hidden}" +
    ".sk-t{clip-path:inset(.17em 0 .17em 0 round 5px)}" +
    ".sk-t.sk-ml{clip-path:none;-webkit-mask-image:repeating-linear-gradient(to bottom,#000 0,#000 calc(var(--sk-lh) - 6px),transparent calc(var(--sk-lh) - 6px),transparent var(--sk-lh));mask-image:repeating-linear-gradient(to bottom,#000 0,#000 calc(var(--sk-lh) - 6px),transparent calc(var(--sk-lh) - 6px),transparent var(--sk-lh))}" +
    ".sk-b>*{visibility:hidden}.sk-b::placeholder{color:transparent}" +
    "@keyframes sk-shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}" +
    "@media (prefers-reduced-motion:reduce){.sk-t,.sk-b{animation:none}}";

  // Varying widths make the bones read as text instead of a column of identical bars.
  var FACTORS = [1, 0.8, 0.9, 0.66, 0.86, 0.74];
  var REG = typeof WeakMap === "function" ? new WeakMap() : null;

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function visible(el) {
    return !!el && el.getClientRects().length > 0;
  }

  function saveStyle(el) {
    return el.getAttribute("style");
  }
  function restoreStyle(el, prev) {
    if (prev === null) el.removeAttribute("style");
    else el.setAttribute("style", prev);
  }

  function textRect(node) {
    var r = document.createRange();
    r.selectNodeContents(node);
    return r.getBoundingClientRect();
  }

  function mask(root, opts) {
    if (!root) return function () {};
    ensureStyle();
    if (REG && REG.has(root)) unmask(root);
    var vary = !opts || opts.vary !== false;

    // ---- pass 1: collect + measure (no DOM changes, so every measurement is of the real layout)
    var texts = [], fields = [], replaced = [];
    var tw = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, null);
    var n;
    while ((n = tw.nextNode())) {
      if (n.nodeType === 3) {
        var p = n.parentElement;
        if (!p || !n.nodeValue.trim()) continue;
        if (/^(SCRIPT|STYLE|NOSCRIPT)$/.test(p.tagName)) continue;
        if (p.closest("svg") || p.closest("[data-sk-skip]") || !visible(p)) continue;
        var rr = textRect(n);
        if (!rr.width) continue;
        var only = Array.prototype.every.call(p.childNodes, function (c) {
          return c === n || c.nodeType === 8 || (c.nodeType === 3 && !c.nodeValue.trim());
        });
        var pcs = getComputedStyle(p), lh = parseFloat(pcs.lineHeight);
        if (!(lh > 0)) lh = parseFloat(pcs.fontSize) * 1.25;
        texts.push({
          node: n, parent: p, only: only, lh: lh,
          w: rr.width, h: only ? p.getBoundingClientRect().height : rr.height
        });
      } else {
        var t = n.tagName;
        if (n.closest("[data-sk-skip]") || !visible(n)) continue;
        if (/^(IMG|CANVAS|VIDEO)$/.test(t) || (t.toLowerCase() === "svg" && !n.parentElement.closest("svg"))) {
          var b = n.getBoundingClientRect();
          replaced.push({ el: n, w: b.width, h: b.height, cs: getComputedStyle(n) });
        } else if (/^(INPUT|TEXTAREA|SELECT)$/.test(t) && n.type !== "hidden") {
          var f = n.getBoundingClientRect();
          fields.push({ el: n, w: f.width, h: f.height });
        }
      }
    }

    // ---- pass 2: mutate
    var recs = [], i = 0;

    texts.forEach(function (x) {
      var w = Math.max(18, x.w * (vary ? FACTORS[i++ % FACTORS.length] : 1));
      var target, rec;
      if (x.only) {
        target = x.parent;
        rec = { type: "text-only", el: target, style: saveStyle(target), node: x.node };
        target.textContent = "";
      } else {
        target = document.createElement("span");
        x.node.replaceWith(target);
        rec = { type: "text-wrapped", el: target, node: x.node };
      }
      if (getComputedStyle(target).display === "inline") {
        target.style.display = "inline-block";
        target.style.verticalAlign = "middle";
      }
      target.style.width = w + "px";
      target.style.height = Math.max(10, x.h) + "px";
      target.style.maxWidth = "100%";
      target.classList.add("sk-t");
      if (x.h > x.lh * 1.6) {            // wrapped text: draw one bar per line instead of one big block
        target.classList.add("sk-ml");
        target.style.setProperty("--sk-lh", x.lh + "px");
        target.style.width = Math.max(w, x.w * 0.92) + "px";
      }
      recs.push(rec);
    });

    replaced.forEach(function (x) {
      var ph = document.createElement("div");
      ph.className = "sk-b";
      ph.style.cssText =
        "display:" + (x.cs.display === "inline" ? "inline-block" : x.cs.display) +
        ";width:" + x.w + "px;height:" + x.h + "px;flex:none;margin:" + x.cs.margin +
        ";--sk-r:" + (x.cs.borderRadius && x.cs.borderRadius !== "0px" ? x.cs.borderRadius : "6px");
      var prev = saveStyle(x.el);
      x.el.parentNode.insertBefore(ph, x.el);
      x.el.style.display = "none";
      recs.push({ type: "replaced", el: x.el, ph: ph, style: prev });
    });

    fields.forEach(function (x) {
      recs.push({ type: "field", el: x.el, style: saveStyle(x.el) });
      x.el.style.width = x.w + "px";
      x.el.style.height = x.h + "px";
      x.el.classList.add("sk-b");
    });

    var prevInert = root.inert, prevBusy = root.getAttribute("aria-busy");
    root.setAttribute("aria-busy", "true");
    root.inert = true;

    var state = { recs: recs, inert: prevInert, busy: prevBusy };
    if (REG) REG.set(root, state);
    return function () { unmask(root); };
  }

  function unmask(root) {
    var st = REG && REG.get(root);
    if (!st) return;
    for (var k = st.recs.length - 1; k >= 0; k--) {
      var r = st.recs[k];
      if (r.type === "text-wrapped") {
        r.el.replaceWith(r.node);
      } else if (r.type === "text-only") {
        r.el.classList.remove("sk-t");
        r.el.textContent = "";
        r.el.appendChild(r.node);
        restoreStyle(r.el, r.style);
      } else if (r.type === "replaced") {
        r.ph.remove();
        restoreStyle(r.el, r.style);
      } else if (r.type === "field") {
        r.el.classList.remove("sk-b");
        restoreStyle(r.el, r.style);
      }
    }
    root.inert = !!st.inert;
    if (st.busy === null) root.removeAttribute("aria-busy");
    else root.setAttribute("aria-busy", st.busy);
    REG.delete(root);
  }

  function maskAll(scope) {
    scope = scope || document;
    var offs = [];
    var zones = Array.prototype.slice.call(scope.querySelectorAll("[data-skeleton]"));
    if (scope.matches && scope.matches("[data-skeleton]")) zones.unshift(scope);
    zones.forEach(function (z) { offs.push(mask(z)); });
    return function () { offs.forEach(function (f) { f(); }); };
  }

  global.Skeleton = { version: "1.0.0", mask: mask, unmask: unmask, maskAll: maskAll };
})(window);