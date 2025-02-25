```
% ./pyreqs.py requests
Processed 5 unique packages.
- requests
  - charset-normalizer<4,>=2
  - idna<4,>=2.5
  - urllib3<3,>=1.21.1
  - certifi>=2017.4.17
```

```
% ./pyreqs.py requests --license
Processed 5 unique packages.
- requests [Apache-2.0]
  - charset-normalizer<4,>=2 [MIT]
  - idna<4,>=2.5 [Unknown]
  - urllib3<3,>=1.21.1 [Unknown]
  - certifi>=2017.4.17 [MPL]

LICENSE REPORT
==============
Total packages with license info: 5

License distribution:
  Apache-2.0: 1 packages
  MIT: 1 packages
  MPL: 1 packages
  Unknown: 2 packages

Detailed license information:

- certifi
  License: MPL
  Project URL: https://pypi.org/project/certifi/
  Author: Kenneth Reitz (me@kennethreitz.com)

- charset-normalizer
  License: MIT
  Project URL: https://pypi.org/project/charset-normalizer/

- idna
  License: Unknown
  Project URL: https://pypi.org/project/idna/

- requests
  License: Apache-2.0
  Project URL: https://pypi.org/project/requests/
  Author: Kenneth Reitz (me@kennethreitz.org)

- urllib3
  License: Unknown
  Project URL: https://pypi.org/project/urllib3/
```
