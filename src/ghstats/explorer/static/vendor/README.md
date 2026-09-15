# Vendored, not fetched

`chart.umd.min.js` is Chart.js 4.4.7 (MIT), taken verbatim from
`https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js`.

`codemirror.js` and `codemirror.css` are CodeMirror 5.65.21 (MIT), the SQL
page's editor. They are unmodified files from the npm tarball
`https://registry.npmjs.org/codemirror/-/codemirror-5.65.21.tgz`
(`sha512-6teYk0bA0nR3QP0ihGMoxuKzpl5W80FpnHpBJpgy66NK3cZv5b/d/HY8PnRvfSsCG1MTfr92u2WUl+wT0E40mQ==`),
concatenated so the page loads one script and one stylesheet:

```bash
cat lib/codemirror.js mode/sql/sql.js addon/edit/matchbrackets.js \
    addon/hint/show-hint.js addon/hint/sql-hint.js > codemirror.js
cat lib/codemirror.css addon/hint/show-hint.css > codemirror.css
```

Version 5 rather than 6 on purpose: 6 ships only as ES modules and needs a
bundler, and this directory has no build step.

It is committed rather than linked because everything downstream of
`ghstats-sync` is offline by contract: the explorer reads a local store over
loopback, and a chart that only draws when a CDN is reachable would make the
one online moment in the pipeline the moment you look at the results. It also
keeps the version pinned to something a `git log` can answer.

To update: replace the file, change the version here, and check the explorer's
charts still draw (or, for CodeMirror, that the SQL page still edits and completes). This vendored copy is now the only one -- the static HTML
report that loaded the same version from a CDN has been removed.

## License

Chart.js is MIT licensed. The minified file carries its copyright line but not
the full notice, which MIT requires be distributed with the code, so it is
reproduced here in full:

```
Copyright (c) 2014-2024 Chart.js Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

CodeMirror is MIT licensed; its notice, reproduced from the tarball's
`LICENSE`:

```
MIT License

Copyright (C) 2017 by Marijn Haverbeke <marijn@haverbeke.berlin> and others

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

The rest of this project is Apache-2.0; see the top-level `LICENSE`.
