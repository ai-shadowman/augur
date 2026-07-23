from abc import ABC, abstractmethod


class CustomEvaluator(ABC):

    @abstractmethod
    def evaluate(self, input: str, graphrag_source_dir: str):
        """
        Provides an abstract method for providing LLM-as-judge
        reference-guided evaluations
        for input data, using a graphrag index file directory as its data
        source.

        For the evaluator models, it will pull from the environment
        configuration. It will use the configured GROUND_TRUTH_LLM for
        reference-based metrics and the configured JUDGE_LLM for judgement-based metrics.

        Args:
            input: The input string to be used for evaluation.
            graphrag_source_dir: The directory path containing graph resources
            required for the evaluation process.

        Returns:
            None

        Raises:
            NotImplementedError: This method must be implemented by subclasses.
        """