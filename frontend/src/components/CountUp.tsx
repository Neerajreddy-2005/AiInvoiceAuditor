import { useEffect, useRef, useState } from "react";

/** Animates a figure to its new value, so a change in the data is visible. */
export function CountUp({ value, format = (n) => String(Math.round(n)) }: { value: number; format?: (n: number) => string }) {
  const [shown, setShown] = useState(0);
  const from = useRef(0);

  useEffect(() => {
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
      from.current = value;
      setShown(value);
      return;
    }
    const start = performance.now();
    const origin = from.current;
    let frame = requestAnimationFrame(function tick(now) {
      const progress = Math.min((now - start) / 600, 1);
      const eased = 1 - (1 - progress) ** 4;
      const current = origin + (value - origin) * eased;
      from.current = current;
      setShown(progress === 1 ? value : current);
      if (progress < 1) frame = requestAnimationFrame(tick);
    });
    return () => cancelAnimationFrame(frame);
  }, [value]);

  return <>{format(shown)}</>;
}
