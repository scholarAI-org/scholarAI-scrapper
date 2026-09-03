from datetime import datetime

from scrapy.exceptions import DropItem

from scholarships.utils_ministry import is_deadline_still_valid


class MinistryPipeline:
    """
    Filters Ministry scholarships before they reach the PostgreSQL pipeline.

    Expired scholarships are dropped and will not be stored in the database.
    Extension notices are also handled separately.
    """

    def process_item(self, item, spider):

        # This pipeline is only responsible for Ministry items.
        if item.get("source") != "ministry":
            return item

        # Extension notices are not normal scholarship records.
        if item.get("is_extension"):
            spider.logger.info(
                f"Extension notice skipped: {item.get('title')}"
            )
            raise DropItem("Ministry extension notice")

        # Get the deadline.
        deadline_str = item.get("deadline")

        if deadline_str:
            try:
                deadline_date = datetime.strptime(
                    deadline_str,
                    "%Y-%m-%d"
                ).date()

            except ValueError:
                spider.logger.warning(
                    f" Invalid deadline for {item.get('source_id')}: "
                    f"{deadline_str}"
                )
                raise DropItem("Invalid deadline")

        else:
            deadline_date = None

        # Check whether the scholarship is still active.
        if not is_deadline_still_valid(deadline_date):

            spider.logger.info(
                f"Expired scholarship skipped: "
                f"{item.get('title')} "
                f"(deadline: {deadline_str})"
            )

            raise DropItem("Expired scholarship")

        # If we reach here, the scholarship is valid.
        spider.logger.info(
            f"Active Ministry scholarship: "
            f"{item.get('source_id')}"
        )

        return item