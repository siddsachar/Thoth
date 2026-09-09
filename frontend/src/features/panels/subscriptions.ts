/** Small view subscriptions. Domain state and connection ownership stay outside panels. */
export class PanelSubscriptions {
  private entries = new Map<
    string,
    {
      revision: string | null;
      subscribe: (notify: (revision: string) => void) => () => void;
      stop: (() => void) | null;
      observers: Map<
        symbol,
        {
          visible: boolean;
          notify: (revision: string) => void;
          delivered: string | null;
        }
      >;
    }
  >();
  readonly metrics = {
    subscriptions: 0,
    cleanups: 0,
    notifications: 0,
    active: 0,
    references: 0,
  };

  acquire(
    key: string,
    subscribe: (notify: (revision: string) => void) => () => void,
    notify: (revision: string) => void,
    visible = true,
  ): { setVisible: (visible: boolean) => void; release: () => void } {
    let entry = this.entries.get(key);
    if (!entry) {
      entry = { revision: null, subscribe, stop: null, observers: new Map() };
      this.entries.set(key, entry);
    }
    const token = Symbol(key);
    entry.observers.set(token, { visible, notify, delivered: null });
    this.metrics.references += 1;
    this.reconcile(key);
    const current = entry.observers.get(token);
    if (
      current?.visible &&
      entry.revision !== null &&
      current.delivered !== entry.revision
    ) {
      current.delivered = entry.revision;
      this.metrics.notifications += 1;
      current.notify(entry.revision);
    }
    let released = false;
    return {
      setVisible: (next) => {
        if (released) return;
        const observer = entry.observers.get(token)!;
        observer.visible = next;
        this.reconcile(key);
        if (
          next &&
          entry.revision !== null &&
          observer.delivered !== entry.revision
        ) {
          observer.delivered = entry.revision;
          this.metrics.notifications += 1;
          observer.notify(entry.revision);
        }
      },
      release: () => {
        if (released) return;
        released = true;
        entry.observers.delete(token);
        this.metrics.references -= 1;
        this.reconcile(key);
        if (entry.observers.size === 0) this.entries.delete(key);
      },
    };
  }
  private reconcile(key: string): void {
    const entry = this.entries.get(key);
    if (!entry) return;
    const visible = [...entry.observers.values()].some(
      (observer) => observer.visible,
    );
    if (visible && !entry.stop) {
      // Set a sentinel first because a source may synchronously publish its current revision.
      entry.stop = () => undefined;
      this.metrics.subscriptions += 1;
      this.metrics.active += 1;
      const stop = entry.subscribe((revision) => {
        if (!this.entries.has(key) || !entry.stop) return;
        entry.revision = revision;
        entry.observers.forEach((observer) => {
          if (observer.visible && observer.delivered !== revision) {
            observer.delivered = revision;
            this.metrics.notifications += 1;
            observer.notify(revision);
          }
        });
      });
      if (!this.entries.has(key) || !entry.stop) stop();
      else entry.stop = stop;
    } else if (!visible && entry.stop) {
      const stop = entry.stop;
      entry.stop = null;
      stop();
      this.metrics.cleanups += 1;
      this.metrics.active -= 1;
    }
  }
  dispose(): void {
    this.entries.forEach((entry) => {
      if (entry.stop) {
        entry.stop();
        this.metrics.cleanups += 1;
      }
    });
    this.entries.clear();
    this.metrics.active = 0;
    this.metrics.references = 0;
  }
}
