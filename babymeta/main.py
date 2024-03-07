import asyncio
from pprint import pprint
from typing import TypeVar, Dict, List, Self, Any

from metagpt.actions import UserRequirement
from metagpt.environment import Environment
from metagpt.logs import logger
from metagpt.schema import Message
from metagpt.team import Team
from pydantic import BaseModel, Field

from babymeta.mymetagpt import InstructAction, InstructRole, match_action

ID = int


class TaskList(BaseModel):
    class Task(BaseModel):
        task_name: str = Field(..., description="The name of the task")

    tasks: Dict[ID, Task] = Field(dict(), description="The list of tasks")
    finished_tasks: Dict[ID, Task] = Field(dict(), description="The list of finished tasks")

    @property
    def task_names(self) -> List[str]:
        return [task.task_name for task in self.tasks.values()]

    @property
    def first_task_id(self) -> ID | None:
        if len(self.tasks) == 0:
            return None
        return min(self.tasks.keys())

    @property
    def last_task_id(self) -> ID | None:
        if len(self.tasks) == 0:
            return None
        return max(self.tasks.keys())

    def pop_first_task(self) -> Task | None:
        id = self.first_task_id
        if len(self.tasks) == 0:
            return None
        task = self.tasks[id]
        del self.tasks[id]
        self.finished_tasks[id] = task
        return task


class BabyEnvironment(Environment):
    finshed: bool = False

    objective: str = Field("", description="The objective of the team")
    task_list: TaskList = Field(TaskList(), description="The list of tasks")


class Result(BaseModel):
    result: str = Field(..., description="The result of the last completed task")
    enriched_result: str = Field(..., description="The enriched result of the last completed task by vector context")


class TaskCreator(InstructRole):
    name: str = "TaskCreator"
    profile: str = "TaskCreator"

    class CreateTask(InstructAction):
        PROMPT_TEMPLATE: str = """You are an task creation AI 
that uses the result of an execution agent to create new tasks with the following objective: 
{objective}, 
The last completed task has the result: {result}. 
This result was based on this task description: {task_description}. 
These tasks are finished: {finished_task_list}.
These are incomplete tasks: {task_list}. 
Based on the result, create new tasks to be completed by the AI system that do not overlap with finished and incomplete tasks. 
If you have no new tasks to create, return "All tasks are completed".
Jenerate less than 5 tasks once."""

        class Input(BaseModel):
            last_task: TaskList.Task = Field(..., description="The last completed task")
            result: Result = Field(..., description="The result of the last completed task")

        class Output(BaseModel):
            new_tasks: List[TaskList.Task] = Field(..., description="The new tasks to be completed")

        output_type: TypeVar = Output

        async def run(self, up: Input, env: BabyEnvironment) -> InstructAction.DirectOutput | Message:
            prompt = self.PROMPT_TEMPLATE.format(
                objective=env.objective,
                result=up,
                task_description=up.last_task.task_name,
                task_list=env.task_list.tasks,
                finished_task_list=env.task_list.finished_tasks
            )
            response = await self._aask(prompt)
            new_tasks = await self.parse_output_prompt(response)

            former_last_task_id = env.task_list.last_task_id
            first_new_task_id = former_last_task_id + 1 if former_last_task_id is not None else 0

            for i, task in enumerate(new_tasks.new_tasks):
                env.task_list.tasks[first_new_task_id + i] = task

            print("Current tasks:")
            print("================================================")
            print(env.task_list.model_dump_json(indent=4))
            print("================================================")

            if len(env.task_list.tasks) == 0:
                return Message(
                    content="All tasks are completed",
                    cause_by=TaskCreator.CreateTask,
                    send_to=BabyAGI.__name__
                )
            return InstructAction.DirectOutput(content=new_tasks)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_actions([TaskCreator.CreateTask])
        self._watch([TaskExecutor.ExecuteTask])


class TaskPrioritizer(InstructRole):
    name: str = "TaskPrioritizer"
    profile: str = "TaskPrioritizer"

    class PrioritizeTasks(InstructAction):
        PROMPT_TEMPLATE: str = """You are an task prioritization AI 
    tasked with cleaning the formatting of and re-prioritizing the following tasks:
{task_names}. 
Consider the ultimate objective of your team:{objective}. 
You can delete or combine tasks, incase task is similar.
Return the result as a numbered list, like:
#. First task
#. Second task"""

        class Output(BaseModel):
            new_tasks: List[TaskList.Task] = Field(..., description="Prioritized tasks")

        output_type: TypeVar = Output

        async def run(self, up: Any, env: BabyEnvironment) -> InstructAction.DirectOutput:
            assert len(env.task_list.tasks) != 0, "There are no tasks to prioritize"

            prompt = self.PROMPT_TEMPLATE.format(
                objective=env.objective,
                task_names=env.task_list.task_names,
            )
            response = await self._aask(prompt)
            new_tasks = await self.parse_output_prompt(response)

            former_first_task_id = env.task_list.first_task_id

            env.task_list.tasks = {}
            for i, task in enumerate(new_tasks.new_tasks):
                env.task_list.tasks[former_first_task_id + i] = task

            print("Prioritized tasks:")
            print("================================================")
            print(env.task_list.model_dump_json(indent=4))
            print("================================================")

            return InstructAction.DirectOutput(content=new_tasks)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_actions([TaskPrioritizer.PrioritizeTasks])
        self._watch([TaskCreator.CreateTask])


class TaskExecutor(InstructRole):
    name: str = "TaskExecutor"
    profile: str = "TaskExecutor"

    context_work_id: int = 0

    def get_context_work_id(self):
        self.context_work_id += 1
        return self.context_work_id

    class AskForContext(InstructAction):
        ...

    class ExecuteTask(InstructAction):
        PROMPT_TEMPLATE: str = """You are an AI who performs one task based on the following objective: 
{objective}. 
Your task: 
{task}
Take into account these previously completed tasks:
{context}
Response:"""

        class Output(BaseModel):
            result: str = Field(..., description="The result of the task")

        output_type: TypeVar = Output

        async def run(self, up: Any, env: BabyEnvironment) -> InstructAction.DirectOutput:
            task = env.task_list.pop_first_task()
            assert task is not None, "There are no tasks to execute"

            logger.info(f"Executing task: {task.task_name}")

            context = ""
            prompt = self.PROMPT_TEMPLATE.format(
                objective=env.objective,
                task=task.task_name,
                context=context
            )
            response = await self._aask(prompt)
            return InstructAction.DirectOutput(
                content=TaskCreator.CreateTask.Input(
                    last_task=task,
                    result=Result(
                        result=response,
                        enriched_result=response
                    )
                )
            )

    class FinishTask(InstructAction):
        ...

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_actions([TaskExecutor.ExecuteTask])
        self._watch([TaskPrioritizer.PrioritizeTasks])


class BabyAGI(InstructRole):
    name: str = "BabyAGI"
    profile: str = "BabyAGI"
    max_round: int = 50

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._watch([UserRequirement])

    async def react(self) -> Message:
        logger.info(f"{self.name}: Working")
        message = self.latest_observed_msg

        if eval(match_action(message.cause_by)) == UserRequirement:
            message.send_to = TaskExecutor.__name__
            resp = message
        elif eval(match_action(message.cause_by)) == TaskCreator.CreateTask:
            assert message.content == "All tasks are completed"
            self.rc.env.finshed = True
            resp = Message(
                content="All tasks are completed",
                send_to="Human"
            )
        else:
            raise ValueError(f"Unexpected message: {message.model_dump_json(indent=4)}")

        return resp


async def main(objective: str = "How to input an elephant into a fridge?"):
    print("\033[96m\033[1m" + "\n*****OBJECTIVE*****\n" + "\033[0m\033[0m")
    print(objective)

    team = Team(env=BabyEnvironment(
        objective=objective,
        task_list=TaskList(tasks={0: TaskList.Task(task_name="Develop a task list.")})
    ))
    team.hire([BabyAGI(), TaskCreator(), TaskPrioritizer(), TaskExecutor()])
    team.invest(3.0)
    team.env.publish_message(
        Message(
            role="Human", content="", cause_by=UserRequirement, send_to=BabyAGI.__name__,
            instruct_content=None
        ),
        peekable=False
    )

    while True:
        await team.run(n_round=1)
        if team.env.finshed:
            break


if __name__ == '__main__':
    asyncio.run(main())
