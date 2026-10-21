// `elk-worker.min.js` is CommonJS: on the main thread `module.exports` is
// `{ default: FakeWorker, Worker: FakeWorker }`; inside a worker it exports nothing.
declare module "elkjs/lib/elk-worker.min.js" {
  const elkWorker: { Worker?: new () => Worker } | undefined;
  export default elkWorker;
}
