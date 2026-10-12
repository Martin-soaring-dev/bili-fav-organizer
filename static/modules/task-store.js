/* UI-facing task state: independent of DOM and transport. */
export function createTaskStore() {
  const records = new Map();
  const listeners = new Set();
  const emit = () => listeners.forEach(listener => listener([...records.values()]));
  return {
    subscribe(listener) {
      listeners.add(listener);
      listener([...records.values()]);
      return () => listeners.delete(listener);
    },
    upsert(task) {
      if (!task || typeof task.id !== "string") throw new TypeError("task.id required");
      records.set(task.id, { ...records.get(task.id), ...task });
      emit();
    },
    replace(tasks) {
      records.clear();
      tasks.forEach(task => records.set(task.id, { ...task }));
      emit();
    },
    get(id) { return records.get(id); },
    all() { return [...records.values()]; },
  };
}
