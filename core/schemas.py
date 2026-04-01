from pydantic import BaseModel, Field


class AmazonMetrics(BaseModel):
    OrdersShopped_V2: float = 0.0
    RequestedQuantity_V2: float = 0.0
    PickedUnits_V2: float = 0.0
    AverageUPH_V2: float = 0.0
    LatePicksRate: float = 0.0
    ItemNotFoundRate_V2: float = 0.0
    ItemFoundRate_V2: float = 0.0
    OrderCancellations: float = 0.0
    TimeAvailable_V2: float = 0.0
    PickTimeInSec_V2: float = 0.0

    # Catch-all for extra variables provided by Amazon without crashing
    class Config:
        extra = "ignore"


class AmazonShopperRecord(BaseModel):
    type: str | None = None
    shopperName: str | None = None
    externalId: str | None = None
    shopperProfile: str | None = "NONE"

    # Metrics can either be nested under 'metrics' or occasionally flattened depending on the endpoint.
    metrics: AmazonMetrics = Field(default_factory=AmazonMetrics)

    # Some fields appear at the root level in certain summationMetrics variants
    LatePicksRate: float | None = None
    TimeAvailable_V2: float | None = None
    OrdersShopped_V2: float | None = None
    RequestedQuantity_V2: float | None = None
    PickedUnits_V2: float | None = None
    AverageUPH_V2: float | None = None
    ItemNotFoundRate_V2: float | None = None
    ItemFoundRate_V2: float | None = None
    OrderCancellations: float | None = None

    class Config:
        extra = "ignore"
