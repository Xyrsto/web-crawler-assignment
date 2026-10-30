from web_crawler.models.fetch_failure import FetchFailure
from web_crawler.models.fetch_skipped import FetchSkipped
from web_crawler.models.fetched_page import FetchedPage

type FetchOutcome = FetchedPage | FetchFailure | FetchSkipped
