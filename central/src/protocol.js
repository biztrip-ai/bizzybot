// Message types on the Central <-> bridge WebSocket.
//
// Central -> bridge:  { type: 'event', seq: <number>, event: { type, payload } }
// bridge -> Central:  { type: 'ack', seq: <number> }
//
// The bridge (Python) keeps its own copy of these strings; the contract is
// documented in docs/DESIGN.md.
export const MSG = {
  EVENT: 'event',
  ACK: 'ack',
};
