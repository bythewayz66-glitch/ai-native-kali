/**
 * Pure board logic for the Hermes Kanban drag-and-drop UI.
 *
 * This module deliberately contains **no DOM access**, so it can be unit-tested
 * under plain Node (`node --test tests/test_board_logic.mjs`). The rules here
 * mirror `kanban_core.state_machine` exactly; they exist so the UI can give
 * instant feedback and explain a refusal, while kanban-core stays the single
 * authoritative decider (the UI never assumes its own answer is final).
 */

/** The six lifecycle columns, in board order. */
export const COLUMNS = ['Backlog', 'Assigned', 'Running', 'Review', 'Done', 'Blocked'];

/**
 * Declared legal edges. Mirrors `ALLOWED_TRANSITIONS` in
 * kanban-core/kanban_core/state_machine.py - anything absent is illegal.
 */
export const ALLOWED_TRANSITIONS = {
  Backlog: ['Assigned', 'Blocked'],
  Assigned: ['Running', 'Backlog', 'Blocked'],
  Running: ['Review', 'Blocked'],
  Review: ['Done', 'Running', 'Blocked'],
  Done: [],
  Blocked: ['Backlog', 'Assigned'],
};

/** Stable guard codes, mirroring `GuardCodes`. */
export const GUARD_CODES = {
  SELF_TRANSITION: 'self_transition',
  ILLEGAL_EDGE: 'illegal_edge',
  KILLED: 'card_killed',
  NO_ASSIGNEE: 'no_assignee',
  SCOPE_MISSING: 'scope_missing',
  SCOPE_EXPIRED: 'scope_expired',
  SCOPE_TARGET: 'target_out_of_scope',
  APPROVAL_PENDING: 'approval_pending',
  APPROVAL_REJECTED: 'approval_rejected',
  TIER_NEEDS_APPROVAL: 'tier_requires_approval',
  UNRESOLVED_APPROVAL: 'unresolved_approval_on_done',
};

/** Short, human-readable explanation per guard code (shown in the toast). */
const GUARD_MESSAGES = {
  self_transition: 'The card is already in that column.',
  illegal_edge: 'That move is not part of the card lifecycle.',
  card_killed: 'An operator killed this card, so it is frozen.',
  no_assignee: 'Assign the card to an agent before it can run.',
  scope_missing: 'This card is T2+, so it needs an authorization scope first.',
  scope_expired: 'The card\'s authorization scope has expired.',
  target_out_of_scope: 'A target on this card is outside the authorized scope.',
  approval_pending: 'A human approval is still pending - decide it first.',
  approval_rejected: 'The approval was rejected. Re-request it to proceed.',
  tier_requires_approval: 'This tier needs an approved human gate before running.',
  unresolved_approval_on_done: 'Resolve the open approval before marking Done.',
};

/** Column accent colour, used for the card's left rail. */
export const COLUMN_ACCENTS = {
  Backlog: '#6f7e91',
  Assigned: '#4f9cf0',
  Running: '#f2b544',
  Review: '#b07cf0',
  Done: '#3fbf8f',
  Blocked: '#f0645a',
};

/**
 * Turn a raw guard string (as kanban-core emits it, e.g.
 * `"no_assignee: a card must be assigned before it can run"`) into a readable
 * sentence, preferring the code's own message but keeping the server's detail.
 */
export function formatGuardReason(raw) {
  if (typeof raw !== 'string' || raw.length === 0) return 'The board refused this move.';
  const [code, ...rest] = raw.split(':');
  const detail = rest.join(':').trim();
  const known = GUARD_MESSAGES[code.trim()];
  if (known && detail) return `${known} (${detail})`;
  if (known) return known;
  if (detail) return detail;
  return raw;
}

/** Ordered, de-duplicated list of columns a card may legally move to. */
export function legalColumns(fromColumn) {
  return [...(ALLOWED_TRANSITIONS[fromColumn] || [])];
}

/**
 * Local pre-check for a drop. Returns `{ok, code, message}`.
 *
 * This is an *optimistic* check for instant UI feedback. The authoritative
 * decision always comes from kanban-core's `/can-move`; the caller must still
 * handle a server refusal.
 */
export function canDrop(card, toColumn) {
  if (!card || typeof card !== 'object') {
    return { ok: false, code: GUARD_CODES.ILLEGAL_EDGE, message: 'That is not a card.' };
  }
  if (!COLUMNS.includes(toColumn)) {
    return { ok: false, code: GUARD_CODES.ILLEGAL_EDGE, message: `Unknown column "${toColumn}".` };
  }
  const from = card.column;
  if (card.killed) {
    return { ok: false, code: GUARD_CODES.KILLED, message: GUARD_MESSAGES.card_killed };
  }
  if (from === toColumn) {
    return { ok: false, code: GUARD_CODES.SELF_TRANSITION, message: GUARD_MESSAGES.self_transition };
  }
  if (!legalColumns(from).includes(toColumn)) {
    return {
      ok: false,
      code: GUARD_CODES.ILLEGAL_EDGE,
      message: `${from} → ${toColumn} is not a declared lifecycle edge.`,
    };
  }
  // Guards that only apply on entry to Running / Done.
  if (toColumn === 'Running') {
    if (!card.assignee) {
      return { ok: false, code: GUARD_CODES.NO_ASSIGNEE, message: GUARD_MESSAGES.no_assignee };
    }
    if (card.pending_approval) {
      return { ok: false, code: GUARD_CODES.APPROVAL_PENDING, message: GUARD_MESSAGES.approval_pending };
    }
  }
  if (toColumn === 'Done' && card.pending_approval) {
    return {
      ok: false,
      code: GUARD_CODES.UNRESOLVED_APPROVAL,
      message: GUARD_MESSAGES.unresolved_approval_on_done,
    };
  }
  return { ok: true, code: null, message: null };
}

/** Bucket cards into their columns; every column is present even when empty. */
export function groupByColumn(cards) {
  const out = {};
  for (const column of COLUMNS) out[column] = [];
  for (const card of cards || []) {
    const column = COLUMNS.includes(card.column) ? card.column : 'Backlog';
    out[column].push(card);
  }
  return out;
}

/** Tier → rail colour for the card's tier chip. */
export function tierColour(tier) {
  if (tier >= 3) return '#f0645a';
  if (tier === 2) return '#f2b544';
  if (tier === 1) return '#4f9cf0';
  return '#6f7e91';
}

/**
 * Apply one bus event to a card list, returning a NEW list.
 *
 * Events are how the board stays live without polling: `card.moved` relocates a
 * card, `card.created` appends one, state events patch fields in place.
 */
export function applyEvent(cards, event) {
  if (!event || typeof event !== 'object' || !event.card_id) return cards;
  const list = Array.isArray(cards) ? cards : [];
  const index = list.findIndex((c) => c.card_id === event.card_id);
  const type = event.type;

  if (type === 'card.created') {
    if (index !== -1) return list;
    const payload = event.payload || {};
    const card = payload.card || {
      card_id: event.card_id,
      title: payload.title || '(untitled)',
      column: payload.column || 'Backlog',
      board_id: event.board_id || payload.board_id,
      priority: payload.priority || 'normal',
      tools: [],
      traces: [],
      approvals: [],
    };
    return [...list, card];
  }

  if (index === -1) return list;
  const current = list[index];
  const next = list.slice();

  if (type === 'card.moved') {
    const to = event.to_column || (event.payload || {}).column;
    if (!to) return list;
    next[index] = { ...current, column: to, assignee: (event.payload || {}).assignee ?? current.assignee };
    return next;
  }

  const patch = { ...(event.payload || {}) };
  delete patch.card;
  if (Object.keys(patch).length === 0) return list;
  next[index] = { ...current, ...patch };
  return next;
}

/** Summary counts for the board header. */
export function summarise(cards) {
  const grouped = groupByColumn(cards);
  const counts = {};
  for (const column of COLUMNS) counts[column] = grouped[column].length;
  const list = cards || [];
  return {
    total: list.length,
    counts,
    running: counts.Running,
    blocked: counts.Blocked,
    review: counts.Review,
    gated: list.filter((c) => c.pending_approval).length,
    killed: list.filter((c) => c.killed).length,
    live: list.filter((c) => c.column === 'Running').length,
  };
}

/** Guard reasons from a kanban-core error payload, formatted for display. */
export function refusalReasons(payload) {
  const fallback = ['The board refused this move.'];
  if (!payload) return fallback;
  const raw = payload.reasons || payload.detail || payload.error;
  // An empty reasons array is useless to the operator - an empty toast explains
  // nothing - so fall back to the generic message rather than showing none.
  if (Array.isArray(raw)) return raw.length ? raw.map(formatGuardReason) : fallback;
  if (typeof raw === 'string' && raw.length) return [raw];
  return fallback;
}

export default {
  COLUMNS,
  ALLOWED_TRANSITIONS,
  GUARD_CODES,
  COLUMN_ACCENTS,
  canDrop,
  groupByColumn,
  applyEvent,
  summarise,
  legalColumns,
  formatGuardReason,
  refusalReasons,
  tierColour,
};
