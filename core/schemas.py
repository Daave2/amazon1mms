from pydantic import BaseModel, Field
from typing import Optional, Any, Dict

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
        extra = 'ignore'

class AmazonShopperRecord(BaseModel):
    type: Optional[str] = None
    shopperName: Optional[str] = None
    externalId: Optional[str] = None
    shopperProfile: Optional[str] = 'NONE'
    
    # Metrics can either be nested under 'metrics' or occasionally flattened depending on the endpoint.
    metrics: AmazonMetrics = Field(default_factory=AmazonMetrics)
    
    # Some fields appear at the root level in certain summationMetrics variants
    LatePicksRate: Optional[float] = None
    TimeAvailable_V2: Optional[float] = None
    OrdersShopped_V2: Optional[float] = None
    RequestedQuantity_V2: Optional[float] = None
    PickedUnits_V2: Optional[float] = None
    AverageUPH_V2: Optional[float] = None
    ItemNotFoundRate_V2: Optional[float] = None
    ItemFoundRate_V2: Optional[float] = None
    OrderCancellations: Optional[float] = None

    class Config:
        extra = 'ignore'
