// The only script on the front page: the preview drawer, and the two arrow
// keys that move between editions. Everything else — filters, day navigation,
// the sources page — is a link the server answers, so the paper still works
// with scripting off.
(function () {
  "use strict";
  var scrim = document.getElementById("preview");
  if (!scrim) return;

  var el = {
    byline: document.getElementById("pv-byline"),
    title: document.getElementById("pv-title"),
    image: document.getElementById("pv-image"),
    dek: document.getElementById("pv-dek"),
    read: document.getElementById("pv-read"),
    links: document.getElementById("pv-links")
  };
  var opener = null;

  function open(card) {
    var d = card.dataset;
    // The byline is cloned rather than rebuilt: it already carries the right
    // favicon and medium glyph, and duplicating that markup here would be one
    // more place to keep in step with the macro.
    var mark = card.querySelector(".byline");
    el.byline.innerHTML = mark ? mark.innerHTML : "";
    el.title.textContent = (card.querySelector(".hlt") || {}).textContent || "";
    el.dek.textContent = d.dek || "";
    el.dek.hidden = !d.dek;
    if (d.image) { el.image.src = d.image; el.image.hidden = false; }
    else { el.image.removeAttribute("src"); el.image.hidden = true; }
    el.read.href = d.read || "#";
    el.read.hidden = !d.read;
    // One button per chip in the byline — the discussion and the link for a
    // Hacker News story, the source alone for a feed. Read from the card, so
    // the source type's decision is made once, in the template.
    el.links.textContent = "";
    var chips = card.querySelectorAll(".byline a.src");
    for (var i = 0; i < chips.length; i++) {
      var a = document.createElement("a");
      a.className = "btn quiet";
      a.href = chips[i].href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      if (chips[i].title) a.title = chips[i].title;
      a.textContent = chips[i].textContent.trim() + " ↗";
      el.links.appendChild(a);
    }
    opener = card.querySelector(".pv-open");
    scrim.hidden = false;
    document.body.style.overflow = "hidden";
    document.querySelector(".drawer-close").focus();
  }

  function close() {
    scrim.hidden = true;
    document.body.style.overflow = "";
    if (opener) { opener.focus(); opener = null; }
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".pv-open");
    if (btn) {
      // The opener is the title link, whose href is the article itself. A
      // modifier click means "open it in a tab": leave that to the browser.
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      open(btn.closest(".hl"));
      return;
    }
    // Clicking the scrim, or the ✕, closes. Clicking inside the drawer does not.
    if (e.target === scrim || e.target.closest(".drawer-close")) close();
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !scrim.hidden) return close();
    if (!scrim.hidden) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var step = e.key === "ArrowLeft" ? "nav-prev"
             : e.key === "ArrowRight" ? "nav-next" : null;
    var link = step && document.getElementById(step);
    if (link) window.location.href = link.href;
  });
})();
