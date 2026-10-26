"""Every part of the HR portal beyond its foundation, in one place. Importing a part adds it to
service.PARTS (its tables, background work, migration check) and registers its routes, person-page
sections, settings, to-dos and kinds of message and document, so this is imported before an
HRService is made (main.py does). The documents part comes first (the others keep their files
there), then the point system (policy, points), which discipline and probation read.
"""

from . import documents, policy, points, certificates, probation, discipline, reviews  # noqa: F401
