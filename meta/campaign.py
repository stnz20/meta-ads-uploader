from facebook_business.adobjects.campaign import Campaign


def get_campaign(campaign_id: str) -> dict:
    campaign = Campaign(campaign_id)
    fields = [
        Campaign.Field.name,
        Campaign.Field.objective,
        Campaign.Field.status,
        Campaign.Field.special_ad_categories,
    ]
    return campaign.api_get(fields=fields).export_all_data()
