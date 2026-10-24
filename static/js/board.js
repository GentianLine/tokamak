// Board interactions for the linear Tokamak stream.
//
// Clicking a ticket row opens its detail page, unless the click hit a
// control (button/input/link) or belongs to a drag gesture. There is no
// kanban drop-target logic anymore: state changes happen through the
// Unicorn component's buttons, which walk the legal transition graph via
// tokamak.services — so Django signals fire on every hop, exactly like
// the REST API path.

// Click on a ticket row body navigates to its detail page, unless the
// click hit a control (button/input/link) or is part of a drag gesture.
document.addEventListener("click", (e) => {
  const card = e.target.closest && e.target.closest(".ticket[data-ticket-id]");
  if (!card) return;
  if (e.target.closest("button, a, input, select")) return;   // controls win
  if (window.__tokamakDragging) return;                          // ignore drag-end clicks
  const id = card.dataset.ticketId;
  if (id) window.location.assign(`/tickets/${id}/`);
});

let __dragTimer = null;
document.addEventListener("dragstart", () => {
  window.__tokamakDragging = true;
  clearTimeout(__dragTimer);
});
document.addEventListener("dragend", () => {
  // Clear a tick later so the click that follows dragend is ignored.
  __dragTimer = setTimeout(() => { window.__tokamakDragging = false; }, 200);
});

// Drag-reorder within a display group. Rows are draggable:
// dropping a row onto another row of the SAME state group reorders via the
// Unicorn component's `reorder` action, which persists through
// tokamak.reorder.reorder_ticket (never touching state, never bypassing
// the service layer). Drops on rows of a different state are ignored —
// workflow moves stay on the explicit buttons/API.
document.addEventListener("dragstart", (e) => {
  const card = e.target.closest && e.target.closest(".ticket[data-ticket-id]");
  if (!card) return;
  // Only stream rows participate; controls inside a row are not handles.
  if (e.target.closest("button, a, input, select")) { e.preventDefault(); return; }
  e.dataTransfer.setData("text/tokamak-ticket", String(card.dataset.ticketId));
  card.classList.add("dragging");
});
document.addEventListener("dragover", (e) => {
  const row = e.target.closest && e.target.closest(".ticket[data-ticket-id]");
  if (!row || !window.__tokamakSameGroup(row)) return;
  e.preventDefault();                       // allow drop inside our group
  row.classList.add("dragover");
});
document.addEventListener("dragleave", (e) => {
  const row = e.target.closest && e.target.closest(".ticket[data-ticket-id]");
  if (row) row.classList.remove("dragover");
});
document.addEventListener("drop", (e) => {
  const row = e.target.closest && e.target.closest(".ticket[data-ticket-id]");
  if (!row) return;
  e.preventDefault();
  row.classList.remove("dragover");
  const movedId = e.dataTransfer.getData("text/tokamak-ticket");
  if (!movedId || String(row.dataset.ticketId) === movedId) return;
  if (!window.__tokamakSameGroup(row, movedId)) return;   // cross-group drops ignored
  // Index of the drop target within its group's DOM order = new index.
  const group = row.closest(".board").querySelectorAll(
    `.ticket[data-state="${row.dataset.state}"]`
  );
  let index = 0;
  group.forEach((r, i) => {
    if (r === row) index = i;
  });
  const view = window.__tokamakBoardComponent;
  if (view && typeof view.callReorder === "function") {
    view.callReorder(movedId, index);
  } else {
    // Fallback: straight REST call when the component hook is unavailable.
    fetch("/api/reorder/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket: Number(movedId), position: index }),
    }).catch(() => {});
  }
});
document.addEventListener("dragend", (e) => {
  const card = e.target.closest && e.target.closest(".ticket[data-ticket-id]");
  if (card) card.classList.remove("dragging");
});

// Same display-group test: identical state pill group (active/review/
// backlog). Cross-group drags are ignored client-side so nothing ever
// mutates workflow state through a reorder gesture.
window.__tokamakSameGroup = function (row, movedId) {
  if (!row || !row.dataset.state) return false;
  if (!movedId) return true;               // dragover probe: state group only
  const cards = row.closest(".board").querySelectorAll(
    `.ticket[data-ticket-id="${movedId}"]`
  );
  const moved = cards.length ? cards[0] : null;
  return !!moved && moved.dataset.state === row.dataset.state;
};
