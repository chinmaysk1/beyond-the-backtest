// Next injects these ambient declarations during `next build`, but a bare
// `tsc --noEmit` (used for typechecking in isolation) does not see them.
declare module '*.css';
