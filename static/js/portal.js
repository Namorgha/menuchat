// Switch-style forms (open/closed, sold out) post in the background and update in place.
// Without JS they submit normally and the server redirects back.
document.addEventListener("submit", async e => {
  const form = e.target.closest("form[data-switch]");
  if (!form) return;
  e.preventDefault();
  const button = form.querySelector("[role=switch]");
  button.disabled = true;
  try {
    const r = await fetch(form.action, {
      method: "POST", credentials: "same-origin", headers: { Accept: "application/json" }, body: new FormData(form),
    });
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    button.setAttribute("aria-checked", String(d.on));
    button.querySelector(".toggle-label").textContent = d.label;
  } catch {
    alert("Couldn't save that change. Check your connection and try again.");
  } finally {
    button.disabled = false;
  }
});

// Fade out success messages.
setTimeout(() => document.querySelectorAll(".flash.success").forEach(el => el.classList.add("hide")), 4000);
