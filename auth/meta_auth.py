from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount
import config


def init_meta_api() -> AdAccount:
    FacebookAdsApi.init(
        app_id=config.META_APP_ID,
        app_secret=config.META_APP_SECRET,
        access_token=config.META_ACCESS_TOKEN,
    )
    return AdAccount(config.META_AD_ACCOUNT_ID)
