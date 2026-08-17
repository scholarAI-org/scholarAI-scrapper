import re
from datetime import datetime


def fix_arabic_line_order(text):
    """
    Reverses the character order of each Arabic text chunk in a line,
    while leaving numbers and Latin text untouched. Needed because
    pdfplumber sometimes extracts Arabic text in reversed order.
    """
    lines = text.split("\n")
    fixed_lines = []
    for line in lines:
        def reverse_arabic_chunk(match):
            return match.group(0)[::-1]
        fixed_line = re.sub(r'[\u0600-\u06FF\s]+', reverse_arabic_chunk, line)
        fixed_lines.append(fixed_line)
    return "\n".join(fixed_lines)


def extract_deadline_from_text(text):
    """
    Searches free-form text for a date in day/month/year order, handling
    the fact that the Ministry's PDFs are inconsistent about which
    number comes first (day, month, or even year). Tries two patterns:
    the standard "d/m/y" format, and a fallback for cases where the
    numbers are separated by whitespace or line breaks instead of "/".
    """
    pattern1 = r'(\d{1,4})\s*/\s*(\d{1,4})\s*[/\n\s]+\s*(\d{1,4})'
    pattern2 = r'(\d{4})\s+(\d{1,2})\s+(\d{1,2})(?!\d)'

    for pattern in [pattern1, pattern2]:
        for match in re.finditer(pattern, text):
            nums = [int(match.group(i)) for i in (1, 2, 3)]
            year_candidates = [n for n in nums if n > 1000]
            if len(year_candidates) != 1:
                continue
            year = year_candidates[0]
            others = [n for n in nums if n != year]
            candidates = []
            for month, day in [(others[0], others[1]), (others[1], others[0])]:
                try:
                    candidates.append(datetime(year, month, day))
                except ValueError:
                    pass
            if not candidates:
                continue
            if len(candidates) == 1:
                return candidates[0]
            # Both orderings are valid dates; assume the one closer to
            # today is more likely correct (scholarship deadlines are
            # usually within weeks/months of the posting date).
            today = datetime.now()
            candidates.sort(key=lambda d: abs((d - today).days))
            return candidates[0]
    return None


def is_deadline_still_valid(deadline_date):
    """Returns True if the deadline hasn't passed yet, or is unknown (None)."""
    if deadline_date is None:
        return True
    return deadline_date >= datetime.now()


def is_extension_post(title):
    """Detects whether a post is a deadline-extension notice rather than
    a new scholarship announcement, based on its title."""
    return title.strip().startswith("تمديد التقديم")