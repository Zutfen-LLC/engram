/** Eslint config for the Portal. Extends Next.js conventions. */
import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({ baseDirectory: __dirname });

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    rules: {
      // Portal is server-rendered; allow server-side data fetching patterns.
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
];

export default eslintConfig;
