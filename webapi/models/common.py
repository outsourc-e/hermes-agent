from pydantic import BaseModel, ConfigDict


class WebAPIModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
