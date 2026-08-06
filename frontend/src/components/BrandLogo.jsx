import { useState } from 'react';

/**
 * IAIRO wordmark. Tries the image file in order (avif → png → webp), falls back
 * to bold text if none of them are in /public. Drop the actual logo into
 *   frontend/public/iairo-logo.avif  (or .png / .webp)
 * and it just appears — no code changes needed for future format swaps.
 */
export default function BrandLogo({ size = 26 }) {
  const [failed, setFailed] = useState(false);
  if (failed) return <span className="brand-iairo">IAIRO</span>;
  return (
    <picture>
      <source srcSet="/iairo-logo.avif" type="image/avif" />
      <source srcSet="/iairo-logo.webp" type="image/webp" />
      <source srcSet="/iairo-logo.png" type="image/png" />
      <img
        src="/iairo-logo.avif"
        alt="IAIRO"
        className="brand-logo-img"
        style={{ height: size }}
        onError={() => setFailed(true)}
      />
    </picture>
  );
}