# Libraries the dashboard serves itself

The dashboard page loads these files from this folder, never from the
internet. Its Content-Security-Policy allows scripts, styles and fonts from
the dashboard itself only, so a file on another server cannot run in the page
and the page works on a network that blocks the CDNs.

The files are byte-for-byte copies of the published releases. They were
downloaded on 17 November 2026 and checked before they were copied here:
Chart.js against the hash jsDelivr publishes for the file (and against the
same file from unpkg), Font Awesome against the hashes (SRI) cdnjs publishes.
`tests/test_the_dashboard_serves_its_own_libraries.py` checks every file
below against its sha256, so a changed file fails the tests.

| File | Version | Source | sha256 |
| --- | --- | --- | --- |
| `chartjs/chart.umd.js` | Chart.js 4.4.1 | `https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js` | `74401d738dd3e03ee5dfb3b6841210fe2c4ead8a960c4011ca4ba0b78a9fd8f3` |
| `fontawesome/css/all.min.css` | Font Awesome Free 6.4.0 | `https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css` | `1edb1725a9ea8ca4dcf2f5508cee183218aa1685e47c1b23056717f754f58ebf` |
| `fontawesome/webfonts/fa-brands-400.woff2` | Font Awesome Free 6.4.0 | `https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-brands-400.woff2` | `748332090c4b8e20f95d0ff59f0be20fa9c889359d3b36d4b886d73376054207` |
| `fontawesome/webfonts/fa-regular-400.woff2` | Font Awesome Free 6.4.0 | `https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-regular-400.woff2` | `8e7e5ea1b15f62ab14dbd41768e8fbcd21cc859a4ea5da812457ee714299fb35` |
| `fontawesome/webfonts/fa-solid-900.woff2` | Font Awesome Free 6.4.0 | `https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-solid-900.woff2` | `7152a6933ee3d690ec2af3d09da9d701723d16aa3410a6d80f28ff8866f3b880` |
| `fontawesome/webfonts/fa-v4compatibility.woff2` | Font Awesome Free 6.4.0 | `https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/fa-v4compatibility.woff2` | `694a17c3d9d6c05f8aac63c544615552a4b220e9a4de863d87341a6bcfc1bc8d` |

The same files in the forms the publishers list them:

| File | Published form | Value |
| --- | --- | --- |
| `chartjs/chart.umd.js` | jsDelivr, sha256 (base64) | `dEAdc43T4D7l37O2hBIQ/ixOrYqWDEARykugt4qf2PM=` |
| `fontawesome/css/all.min.css` | cdnjs SRI | `sha512-iecdLmaskl7CVkqkXNQ/ZH/XLlvWZOJyj7Yy7tcenmpD1ypASozpmT/E0iPtmFIB46ZmdtAc9eNBvH0H/ZpiBw==` |
| `fontawesome/webfonts/fa-brands-400.woff2` | cdnjs SRI | `sha512-07R5G5iPz9mRGiFYFj0MRNZ5dlCJC11Kx2lBfgnY/Cxn7cWVvo55J94FGahe6zV30Mfjhb3JnXYsemz7rQIbOQ==` |
| `fontawesome/webfonts/fa-regular-400.woff2` | cdnjs SRI | `sha512-22+C27/Qx30bNAdO0u6qhlmSSrGkMg/e7pPuw2q5hRb9koNnZc7QPwrLcNFTrBTEtQt9/MjxBamV/E5lv/vc0Q==` |
| `fontawesome/webfonts/fa-solid-900.woff2` | cdnjs SRI | `sha512-DM3BUVUQ2QLAtKSLhjxIuthuH3ZrH5yJCmTijZHufG1IgkHFMfwJTRWynCEdpx4JJYeph+JO6OZ++OqZwoToIQ==` |
| `fontawesome/webfonts/fa-v4compatibility.woff2` | cdnjs SRI | `sha512-ePmt4sJ26QfXKZLLCF+waJLZ8ajmT9fY3Uw6fZj04hrYYQ9xfkDD6kkIUExFDG4gXmQKGhFybutU0ooku6QylQ==` |

`all.min.css` names each font twice: the `.woff2` file and an older `.ttf`
one. Every browser in use today takes the `.woff2` file, so the `.ttf` files
are not here. Keep the folder layout: the stylesheet finds the fonts at
`../webfonts/`.

## Licences

- **Chart.js**: MIT licence, (c) 2023 Chart.js Contributors. The licence
  line is at the top of `chart.umd.js`.
- **Font Awesome Free**: the icons are under CC BY 4.0, the fonts under the
  SIL Open Font License 1.1, and the code (the stylesheet) under the MIT
  licence (`https://fontawesome.com/license/free`). The attribution CC BY
  asks for is the notice at the top of `all.min.css`; keep it.

## Changing a version

1. Download the new files from the same sources and check them against the
   hashes the publisher lists.
2. Replace the files here, keeping the names and the layout, and update both
   tables above.
3. Put the new sha256 values in `VENDORED` in
   `tests/test_the_dashboard_serves_its_own_libraries.py` too: the test checks
   each file against its own list and checks that this table says the same.
   Then run the whole test suite and the browser tests
   (`python -m pytest -m ui`).
