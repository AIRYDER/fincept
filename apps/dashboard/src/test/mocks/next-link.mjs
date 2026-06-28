/**
 * Test mock for next/link.
 *
 * Renders a plain <a> tag so components that use Link render without
 * requiring the Next.js Link infrastructure.
 */
import React from "react";

export function Link(props) {
  const { href, children, ...rest } = props;
  return React.createElement("a", { href, ...rest }, children);
}

export default Link;
