import { useEffect, useState } from "react";

/** ``value`` once it has not changed for ``ms`` milliseconds (the first value at once). */
export function useDebounced<T>(value: T, ms: number): T {
  const [current, setCurrent] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setCurrent(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return current;
}
