# Third-party notices

The committed panel bundles (`app/console/console.js` and its byte-identical copy `plugin/dashboard/dist/console/assets/console.js`) are minified builds of `web/src` that include the following packages; `plugin/dashboard/dist/index.js` uses the dashboard's own React and bundles none. Their license comments are stripped by the build, so the notices are kept here. Versions are pinned in `web/package-lock.json`.

| Package | Version | License |
|---|---|---|
| react | 19.1.1 | MIT |
| react-dom | 19.1.1 | MIT |
| scheduler | 0.26.0 | MIT |

```text
MIT License

Copyright (c) Meta Platforms, Inc. and affiliates.

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

The service's Python dependencies are installed from `uv.lock`, not vendored: FastAPI and markdown-it-py (MIT), Uvicorn (BSD-3-Clause), bcrypt (Apache-2.0).
