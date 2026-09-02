'use strict';
/**
 * Removes generated build output ONLY.
 * dist/ is application source and is never touched here.
 */
const fs = require('fs');
const path = require('path');

const OUTPUT_DIRS = ['release'];   // never add the source folder to this list

for (const dir of OUTPUT_DIRS) {
  const target = path.join(__dirname, '..', dir);
  if (fs.existsSync(target)) {
    fs.rmSync(target, { recursive: true, force: true });
    console.log(`removed ${dir}/`);
  } else {
    console.log(`${dir}/ already clean`);
  }
}
