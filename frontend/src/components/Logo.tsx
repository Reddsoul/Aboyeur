/** The Aboyeur mark — a penguin in a toque, black line art on transparency.
 *
 * One <img> rather than an inline SVG because the source is raster; it sits on
 * an opaque plate (see .brand-plate in tokens.css) so the black art always has
 * something light behind it, in either theme. */
export default function Logo({ size = 32, plate = true, className = '' }: { size?: number; plate?: boolean; className?: string }) {
  // Below ~44px the whole bird turns into a dark smudge; the toque-and-beak
  // crop stays legible at avatar size.
  const src = size <= 44 ? '/logo-mark.png' : '/logo.png';
  const img = (
    <img
      src={src}
      alt=""
      width={Math.round(size * (plate ? 0.78 : 1))}
      height={Math.round(size * (plate ? 0.78 : 1))}
      className="brand-mark"
      draggable={false}
    />
  );
  if (!plate) return img;
  return (
    <span
      className={`brand-plate ${className}`}
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.29) }}
      aria-hidden="true"
    >
      {img}
    </span>
  );
}
