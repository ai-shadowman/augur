##############################################################################
# Register custom blocks
##############################################################################
from sdg_hub.core.blocks.base import BaseBlock
from sdg_hub.core.blocks.llm.llm_chat_block import LLMChatBlock
from sdg_hub.core.blocks.registry import BlockRegistry
from pydantic import ConfigDict, field_validator
import validators
from sdg_hub.core.utils.logger_config import setup_logger
from litellm import acompletion, completion
import pandas as pd
from typing import Any, Optional
import asyncio
logger = setup_logger(__name__)
import os

@BlockRegistry.register(
    "CustomDeleteColumnsBlock",
    "transform",
    "Drops columns in a dataset",
)
class CustomDeleteColumnsBlock(BaseBlock):
    """Block for dropping columns in a dataset.

    Attributes
    ----------
    block_name : str
        Name of the block.
    input_cols
    """

    @field_validator("input_cols", mode="after")
    @classmethod
    def validate_input_cols(cls, v):
        """Validate that input_cols is a non-empty dict."""
        if not v:
            raise ValueError("input_cols cannot be empty")
        return v

    def generate(self, samples: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """Generate a dataset with dropped columns.

        Parameters
        ----------
        samples : pd.DataFrame
            Input dataset from which columns will be dropped.

        Returns
        -------
        pd.DataFrame
            Dataset with dropped columns.

        Raises
        ------
        ValueError
            If attempting to drop a column that don't exist in the dataset.
        """
        # Check that all original column names exist in the dataset
        existing_cols = set(samples.columns.tolist())
        droppable_cols = set(self.input_cols)

        missing_cols = droppable_cols - existing_cols
        if missing_cols:
            raise ValueError(
                f"Droppable column names {sorted(missing_cols)} not in the dataset"
            )

        # Drop columns using pandas method
        return samples.drop(columns=self.input_cols)