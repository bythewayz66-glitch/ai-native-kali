/**
 * Unit tests for the board's pure drag-and-drop logic.
 *
 * Run with:  node --test board-ui/tests/test_board_logic.mjs
 *
 * These tests matter because the UI's local check is what decides whether a drop
 * looks legal *before* the server is asked. If it disagreed with
 * kanban_core.state_machine, the board would either block legal moves or invite
 * illegal ones - and the "refusal with reasons" behaviour would be a lie.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  COLUMNS,
  ALLOWED_TRANSITIONS,
  canDrop,
  groupByColumn,
  applyEvent,
  summarise,
  legalColumns,
  formatGuardReason,
  refusalReasons,
} from '../board_ui/static/board_logic.mjs';

const card = (over = {}) => ({ card_id: 'crd_1', title: 't', column: 'Backlog', ...over });

test('the six lifecycle columns are declared in order', () => {
  assert.deepEqual(COLUMNS, ['Backlog', 'Assigned', 'Running', 'Review', 'Done', 'Blocked']);
});

test('the edge table matches the engine contract', () => {
  assert.deepEqual(ALLOWED_TRANSITIONS.Backlog, ['Assigned', 'Blocked']);
  assert.deepEqual(ALLOWED_TRANSITIONS.Running, ['Review', 'Blocked']);
  assert.deepEqual(ALLOWED_TRANSITIONS.Done, [], 'Done must be terminal');
  assert.deepEqual(ALLOWED_TRANSITIONS.Blocked, ['Backlog', 'Assigned']);
});

test('a declared edge is allowed', () => {
  const verdict = canDrop(card({ column: 'Backlog', assignee: 'a' }), 'Assigned');
  assert.equal(verdict.ok, true);
});

test('an undeclared edge is refused with the illegal_edge code', () => {
  const verdict = canDrop(card({ column: 'Backlog' }), 'Review');
  assert.equal(verdict.ok, false);
  assert.equal(verdict.code, 'illegal_edge');
  assert.match(verdict.message, /not a declared lifecycle edge/);
});

test('Done is terminal - nothing may leave it', () => {
  for (const target of COLUMNS) {
    if (target === 'Done') continue;
    const verdict = canDrop(card({ column: 'Done' }), target);
    assert.equal(verdict.ok, false, `Done -> ${target} must be refused`);
  }
});

test('a no-op drop onto the same column is refused', () => {
  const verdict = canDrop(card({ column: 'Running', assignee: 'a' }), 'Running');
  assert.equal(verdict.code, 'self_transition');
});

test('an unassigned card cannot enter Running', () => {
  const verdict = canDrop(card({ column: 'Assigned' }), 'Running');
  assert.equal(verdict.ok, false);
  assert.equal(verdict.code, 'no_assignee');
});

test('an assigned card can enter Running', () => {
  assert.equal(canDrop(card({ column: 'Assigned', assignee: 'recon-specialist' }), 'Running').ok, true);
});

test('a pending approval blocks entry to Running', () => {
  const verdict = canDrop(
    card({ column: 'Assigned', assignee: 'a', pending_approval: { id: 'apr_1' } }),
    'Running',
  );
  assert.equal(verdict.code, 'approval_pending');
});

test('a pending approval blocks Done', () => {
  const verdict = canDrop(card({ column: 'Review', pending_approval: { id: 'apr_1' } }), 'Done');
  assert.equal(verdict.code, 'unresolved_approval_on_done');
});

test('a killed card is frozen in place', () => {
  const verdict = canDrop(card({ column: 'Assigned', assignee: 'a', killed: true }), 'Running');
  assert.equal(verdict.ok, false);
  assert.equal(verdict.code, 'card_killed');
});

test('an unknown column is refused rather than silently accepted', () => {
  assert.equal(canDrop(card(), 'Archived').code, 'illegal_edge');
});

test('a missing card is refused', () => {
  assert.equal(canDrop(null, 'Assigned').ok, false);
});

test('legalColumns reports the same set the engine allows', () => {
  assert.deepEqual(legalColumns('Assigned').sort(), ['Backlog', 'Blocked', 'Running']);
  assert.deepEqual(legalColumns('Done'), []);
});

test('groupByColumn always returns every column, even empty', () => {
  const grouped = groupByColumn([]);
  assert.deepEqual(Object.keys(grouped), COLUMNS);
  for (const col of COLUMNS) assert.deepEqual(grouped[col], []);
});

test('groupByColumn files an unknown column into Backlog rather than dropping the card', () => {
  const grouped = groupByColumn([card({ column: 'Nonsense' })]);
  assert.equal(grouped.Backlog.length, 1);
});

test('applyEvent relocates a card on card.moved', () => {
  const cards = [card({ column: 'Assigned', assignee: 'a' })];
  const next = applyEvent(cards, { type: 'card.moved', card_id: 'crd_1', from_column: 'Assigned', to_column: 'Running' });
  assert.equal(next[0].column, 'Running');
  assert.equal(cards[0].column, 'Assigned', 'the input list must not be mutated');
});

test('applyEvent appends a card on card.created', () => {
  const next = applyEvent([], { type: 'card.created', card_id: 'crd_9', payload: { title: 'New', column: 'Backlog' } });
  assert.equal(next.length, 1);
  assert.equal(next[0].card_id, 'crd_9');
  assert.equal(next[0].title, 'New');
});

test('applyEvent is idempotent for a duplicate card.created', () => {
  const once = applyEvent([], { type: 'card.created', card_id: 'crd_9', payload: { title: 'New' } });
  const twice = applyEvent(once, { type: 'card.created', card_id: 'crd_9', payload: { title: 'New' } });
  assert.equal(twice.length, 1);
});

test('applyEvent ignores an event for an unknown card', () => {
  const cards = [card()];
  assert.equal(applyEvent(cards, { type: 'card.moved', card_id: 'nope', to_column: 'Done' }).length, 1);
});

test('applyEvent ignores a malformed event instead of throwing', () => {
  const cards = [card()];
  assert.deepEqual(applyEvent(cards, null), cards);
  assert.deepEqual(applyEvent(cards, { type: 'card.moved' }), cards);
});

test('summarise counts each column and the flagged cards', () => {
  const cards = [
    card({ card_id: 'a', column: 'Running', assignee: 'x' }),
    card({ card_id: 'b', column: 'Running', assignee: 'y', pending_approval: { id: 'p' } }),
    card({ card_id: 'c', column: 'Blocked', killed: true }),
    card({ card_id: 'd', column: 'Review' }),
  ];
  const sum = summarise(cards);
  assert.equal(sum.total, 4);
  assert.equal(sum.running, 2);
  assert.equal(sum.blocked, 1);
  assert.equal(sum.review, 1);
  assert.equal(sum.gated, 1);
  assert.equal(sum.killed, 1);
});

test('formatGuardReason turns a coded reason into a readable sentence', () => {
  const text = formatGuardReason('no_assignee: a card must be assigned before it can run');
  assert.match(text, /Assign the card to an agent/);
  assert.match(text, /a card must be assigned/);
});

test('formatGuardReason keeps an unknown code legible', () => {
  assert.equal(formatGuardReason('brand_new_guard'), 'brand_new_guard');
  assert.equal(formatGuardReason(''), 'The board refused this move.');
});

test('refusalReasons formats every shape the server may return', () => {
  assert.deepEqual(refusalReasons(null).length, 1);
  assert.equal(refusalReasons({ reasons: ['no_assignee: must be assigned'] })[0].includes('Assign'), true);
  assert.deepEqual(refusalReasons({ detail: 'boom' }), ['boom']);
  assert.deepEqual(refusalReasons({ error: 'nope' }), ['nope']);
  assert.equal(refusalReasons({ reasons: [] }).length, 1);
});
