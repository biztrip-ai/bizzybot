// Message types on the Central-Dispatch <-> agent-wrapper WebSocket.
//
// Central-Dispatch -> agent-wrapper:  { type: 'event', seq: <number>, event: { type, payload } }
// agent-wrapper -> Central-Dispatch:  { type: 'ack', seq: <number> }
//
// The agent-wrapper (Python) keeps its own copy of these strings; the contract is
// documented in docs/DESIGN.md.
export const MSG = {
  EVENT: 'event',
  ACK: 'ack',
};
